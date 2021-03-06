'''Encode target locations and class labels.'''
import math
import torch

from utils import meshgrid, box_iou, box_nms, change_box_order


class DataEncoder:
    def __init__(self):
        self.anchor_areas = [32*32., 64*64., 128*128., 256*256., 512*512.]  # p3 -> p7
        self.aspect_ratios = [1/2., 1/1., 2/1.]
        self.scale_ratios = [1., pow(2,1/3.), pow(2,2/3.)]
        self.anchor_wh = self._get_anchor_wh()

    def _get_anchor_wh(self):
        '''Compute anchor width and height for each feature map.

        Returns:
          anchor_wh: (tensor) anchor wh, sized [#fm, #anchors_per_cell, 2].
        '''
        anchor_wh = []
        for s in self.anchor_areas:
            for ar in self.aspect_ratios:  # w/h = ar
                h = math.sqrt(s/ar)
                w = ar * h
                for sr in self.scale_ratios:  # scale
                    anchor_h = h*sr
                    anchor_w = w*sr
                    anchor_wh.append([anchor_w, anchor_h])
        num_fms = len(self.anchor_areas)
        return torch.Tensor(anchor_wh).view(num_fms, -1, 2)

    def _get_anchor_boxes(self, input_size):
        '''Compute anchor boxes for each feature map.

        Args:
          input_size: (tensor) model input size of (input_height, input_width).

        Returns:
          boxes: (list) anchor boxes for each feature map. Each of size [#anchors,4],
                        where #anchors = fmh * fmw * #anchors_per_cell
        '''
        num_fms = len(self.anchor_areas)
        fm_sizes = [(input_size/pow(2.,i+3)).ceil() for i in range(num_fms)]  # p3 -> p7 feature map sizes
        # TODO: make sure computed fm_sizes is the same as feature_map sizes

        boxes = []
        for i in range(num_fms):
            fm_size = fm_sizes[i]
            grid_size = (input_size/fm_size).floor()
            fm_h, fm_w = int(fm_size[0]), int(fm_size[1])
            xy = meshgrid(fm_w,fm_h) + 0.5  # [fm_h*fm_w,2]
            xy = (xy*grid_size).view(fm_h,fm_w,1,2).expand(fm_h,fm_w,9,2)
            wh = self.anchor_wh[i].view(1,1,9,2).expand(fm_h,fm_w,9,2)
            box = torch.cat([xy,wh], 3)  # [x,y,w,h]
            boxes.append(box.view(-1,4))
        return torch.cat(boxes, 0)

    def encode(self, boxes, labels, input_size):
        '''Encode target bounding boxes and class labels.

        We obey the Faster RCNN box coder:
          tx = (x - anchor_x) / anchor_w
          ty = (y - anchor_y) / anchor_h
          tw = log(w / anchor_w)
          th = log(h / anchor_h)

        Then we scale [tx,ty,tw,th] by [10,10,5,5] times to make loc_loss larger.

        Args:
          boxes: (tensor) bounding boxes of (xmin,ymin,xmax,ymax), sized [#obj, 4].
          labels: (tensor) object class labels, sized [#obj,].
          input_size: (int/tuple) model input size of (input_height, input_width).

        Returns:
          loc_targets: (tensor) encoded bounding boxes, sized [#anchors,4].
          cls_targets: (tensor) encoded class labels, sized [#anchors,].

        Reference:
          https://github.com/tensorflow/models/blob/master/object_detection/box_coders/faster_rcnn_box_coder.py
        '''
        scale_factor = torch.Tensor([10,10,5,5])  # scale [tx,ty,tw,th]
        input_size = torch.Tensor([input_size,input_size]) if isinstance(input_size, int) \
                     else torch.Tensor(input_size)
        anchor_boxes = self._get_anchor_boxes(input_size)
        boxes = change_box_order(boxes, 'xyxy2xywh')

        ious = box_iou(anchor_boxes, boxes, order='xywh')
        max_ious, max_ids = ious.max(1)
        boxes = boxes[max_ids]

        loc_xy = (boxes[:,:2]-anchor_boxes[:,:2]) / anchor_boxes[:,2:]
        loc_wh = torch.log(boxes[:,2:]/anchor_boxes[:,2:])
        loc_targets = torch.cat([loc_xy,loc_wh], 1) * scale_factor
        cls_targets = 1 + labels[max_ids]

        cls_targets[max_ious<0.4] = 0
        ignore = (max_ious>0.4) & (max_ious<0.5)  # ignore ious between [0.4,0.5]
        cls_targets[ignore] = -1  # for now just mark ignored to -1
        return loc_targets, cls_targets

    def decode(self, loc_preds, cls_preds, input_size):
        '''Decode outputs back to bouding box locations and class labels.

        Args:
          loc_preds: (tensor) predicted locations, sized [#anchors, 4].
          cls_preds: (tensor) predicted class labels, sized [#anchors, #classes].
          input_size: (int/tuple) model input size of (input_height, input_width).

        Returns:
          boxes: (tensor) decode box locations, sized [#obj,4].
          labels: (tensor) class labels for each box, sized [#obj,].
        '''
        CLS_THRESH = 0.05
        NMS_THRESH = 0.5
        scale_factor = torch.Tensor([10,10,5,5])  # scale [tx,ty,tw,th]

        input_size = torch.Tensor([input_size,input_size]) if isinstance(input_size, int) \
                     else torch.Tensor(input_size)
        anchor_boxes = self._get_anchor_boxes(input_size)

        loc_preds /= scale_factor
        loc_xy = loc_preds[:,:2]
        loc_wh = loc_preds[:,2:]
        xy = loc_xy * anchor_boxes[:,2:] + anchor_boxes[:,:2]
        wh = loc_wh.exp() * anchor_boxes[:,2:]
        boxes = torch.cat([xy-wh/2, xy+wh/2], 1)  # [#anchors,4]
        boxes[:,0].clamp_(min=0)
        boxes[:,1].clamp_(min=0)
        boxes[:,2].clamp_(max=input_size[1])
        boxes[:,3].clamp_(max=input_size[0])

        score, labels = cls_preds.max(1)          # [#anchors,]
        ids = (score > CLS_THRESH) & (labels > 0)
        ids = ids.nonzero().squeeze()             # [#obj,]
        keep = box_nms(boxes[ids], score[ids], threshold=NMS_THRESH)
        return boxes[ids][keep], labels[ids][keep]


def test():
    in_size = 600
    c3_size = 75
    grid_size = in_size/c3_size
    cx = grid_size/2.
    cy = grid_size/2.
    w = 32
    h = 32
    boxes = torch.Tensor([[cx-w/2.,cy-w/2.,cx+w/2.,cy+w/2.]])
    labels = torch.LongTensor([2])
    encoder = DataEncoder()
    loc_targets, cls_targets = encoder.encode(boxes, labels, input_size=(in_size))
    print(boxes)
    print(labels)
    loc_preds = loc_targets
    cls_preds = torch.zeros(1,21)
    cls_preds[0,3] = 1
    boxes_, labels_ = encoder.decode(loc_preds, cls_preds, input_size=(in_size))
    print(boxes_)
    print(labels_)

def test2():
    line = '335 500 139 200 207 301 18'
    # line = '354 480 87 97 258 427 12 133 72 245 284 14'
    sp = line.strip().split()
    w = float(sp[0])
    h = float(sp[1])
    N = (len(sp)-2)//5
    boxes = []
    labels = []
    for i in range(N):
        boxes.append([float(x) for x in [sp[5*i+2],sp[5*i+3],sp[5*i+4],sp[5*i+5]]])
        labels.append(int(sp[5*i+6]))
    boxes = torch.Tensor(boxes)
    labels = torch.LongTensor(labels)
    encoder = DataEncoder()
    loc_targets, cls_targets = encoder.encode(boxes, labels, input_size=600)

# test()
