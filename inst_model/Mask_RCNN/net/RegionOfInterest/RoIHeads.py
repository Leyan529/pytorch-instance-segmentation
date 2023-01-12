import torch
from torch import nn
import torch.nn.functional as F
from ..Utils import box_iou
from ..Utils import BoxCoder
from ..Utils import process_box
from ..Utils import Matcher
from torchvision.ops.boxes import nms
from ..Utils import BalancedPositiveNegativeSampler
from ..loss import detection_loss, mask_loss


class RoIHeads(nn.Module):
    def __init__(self, box_roi_pool,
                 box_predictor,
                 fg_iou_thresh,
                 bg_iou_thresh,
                 num_samples,
                 positive_fraction,
                 reg_weights,
                 score_thresh,
                 nms_thresh,
                 num_detections,
                 train_mode):

        super().__init__()
        self.box_roi_pool = box_roi_pool
        self.box_predictor = box_predictor
        self.mask_roi_pool = None
        self.mask_predictor = None
        self.train_mode = train_mode
        self.proposal_matcher = Matcher(fg_iou_thresh, bg_iou_thresh)
        self.fg_bg_sampler = BalancedPositiveNegativeSampler(num_samples, positive_fraction)
        self.box_coder = BoxCoder(reg_weights)
        self.score_thresh = score_thresh
        self.nms_thresh = nms_thresh
        self.num_detections = num_detections
        self.min_size = 1

    def select_training_samples(self, proposal, target):
        gt_box = target['boxes']
        gt_label = target['labels']
        proposal = torch.cat((proposal, gt_box))

        iou = box_iou(gt_box, proposal)
        pos_neg_label, matched_idx = self.proposal_matcher(iou)
        pos_idx, neg_idx = self.fg_bg_sampler(pos_neg_label)
        idx = torch.cat((pos_idx, neg_idx))

        regression_target = self.box_coder.encode(gt_box[matched_idx[pos_idx]], proposal[pos_idx])
        proposal = proposal[idx]
        matched_idx = matched_idx[idx]
        label = gt_label[matched_idx]
        num_pos = pos_idx.shape[0]
        label[num_pos:] = 0

        return proposal, matched_idx, label, regression_target

    def detect(self, class_logit, box_regression, proposal, image_shape):
        N, num_classes = class_logit.shape

        device = class_logit.device
        pred_score = F.softmax(class_logit, dim=-1)
        box_regression = box_regression.reshape(N, -1, 4)

        boxes = []
        labels = []
        scores = []
        for image_class in range(1, num_classes):
            score, box_delta = pred_score[:, image_class], box_regression[:, image_class]

            keep = score >= self.score_thresh
            box, score, box_delta = proposal[keep], score[keep], box_delta[keep]
            box = self.box_coder.decode(box_delta, box)

            box, score = process_box(box, score, image_shape, self.min_size)

            keep = nms(box, score, self.nms_thresh)[:self.num_detections]
            box, score = box[keep], score[keep]
            label = torch.full((len(keep),), image_class, dtype=keep.dtype, device=device)

            boxes.append(box)
            labels.append(label)
            scores.append(score)

        results = dict(boxes=torch.cat(boxes), labels=torch.cat(labels), scores=torch.cat(scores))
        return results

    def forward(self, feature, proposal, image_shape, target):
        if self.train_mode:
            proposal, matched_idx, label, regression_target = self.select_training_samples(proposal, target)

        box_feature = self.box_roi_pool(feature, proposal, image_shape)
        class_logit, box_regression = self.box_predictor(box_feature)

        result, losses = {}, {}
        if self.train_mode:
            classifier_loss, box_reg_loss = detection_loss(class_logit, box_regression, label, regression_target)
            losses = dict(roi_classifier_loss=classifier_loss, roi_box_loss=box_reg_loss)
        else:
            result = self.detect(class_logit, box_regression, proposal, image_shape)

        if (self.mask_roi_pool is not None) and (self.mask_predictor is not None):
            if self.train_mode:
                num_pos = regression_target.shape[0]

                mask_proposal = proposal[:num_pos]
                pos_matched_idx = matched_idx[:num_pos]
                mask_label = label[:num_pos]

                if mask_proposal.shape[0] == 0:
                    losses.update(dict(roi_mask_loss=torch.tensor(0)))
                    return result, losses
            else:
                mask_proposal = result['boxes']

                if mask_proposal.shape[0] == 0:
                    result.update(dict(masks=torch.empty((0, 28, 28))))
                    return result, losses

            mask_feature = self.mask_roi_pool(feature, mask_proposal, image_shape)
            mask_logit = self.mask_predictor(mask_feature)

            if self.train_mode:
                gt_mask = target['masks']
                m_loss = mask_loss(mask_logit, mask_proposal, pos_matched_idx, mask_label, gt_mask)
                losses.update(dict(roi_mask_loss=m_loss))
            else:
                label = result['labels']
                idx = torch.arange(label.shape[0], device=label.device)
                mask_logit = mask_logit[idx, label]

                mask_prob = mask_logit.sigmoid()
                result.update(dict(masks=mask_prob))

        return result, losses