from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
from string import ascii_lowercase
import collections
import caffe
from caffe import layers as L, params as P, to_proto



class ResNet(): 
    def __init__(self, stages=[3, 4, 6, 3], channals=64, deploy=False, classes = 2, anchors = 9, feat_stride = 16, pooled_size=[14, 14], module = "normal", pooling = "align"):
        self.stages = stages
        self.channals = channals
        self.deploy = deploy
        self.classes = classes
        self.anchors = anchors
        self.feat_stride = feat_stride
        self.module = module
        self.net = caffe.NetSpec()
        self.pooling = pooling
        self.pooled_w = pooled_size[0] 
        self.pooled_h = pooled_size[1] 

    def roi_align(self, bottom, roi):
        if self.pooling == "align":
            self.net["ROIAlign"] = L.ROIAlign(bottom, roi, roi_align_param = {
                    "pooled_w": self.pooled_w,
                    "pooled_h": self.pooled_h,
                    "spatial_scale": 1/float(self.feat_stride)})
            return self.net["ROIAlign"]
        else:
            self.net["ROIPooling"] = L.ROIPooling(bottom, roi, roi_pooling_param = {
                    "pooled_w": self.pooled_w,
                    "pooled_h": self.pooled_h,
                    "spatial_scale": 1/float(self.feat_stride)})
            return self.net["ROIPooling"]

    def conv_factory(self, name, bottom, ks, nout, stride=1, pad=0, bias_term=False):
        self.net[name] = L.Convolution(bottom, kernel_size=ks, stride=stride,
                                    num_output=nout, pad=pad, bias_term=bias_term, weight_filler=dict(type='msra'))
        if "res" in name:
            self.net[name.replace("res", "bn")] = L.BatchNorm(self.net[name], in_place=True, batch_norm_param=dict(use_global_stats=self.deploy))
            self.net[name.replace("res", "scale")]  = L.Scale(self.net[name.replace("res", "bn")], in_place=True, scale_param=dict(bias_term=True))
            self.net[name + "_relu"] = L.ReLU(self.net[name.replace("res", "scale")], in_place=True)
        else:
            self.net["bn_" + name] = L.BatchNorm(self.net[name], in_place=True, batch_norm_param=dict(use_global_stats=self.deploy))
            self.net["scale_" + name] = L.Scale(self.net["bn_" + name], in_place=True, scale_param=dict(bias_term=True))
            self.net[name + "_relu"] = L.ReLU(self.net["scale_" + name], in_place=True)
        return self.net[name + "_relu"]

    def conv_factory_inverse_no_relu(self, name, bottom, ks, nout, stride=1, pad=0, deploy=False, bias_term=False):
        self.net[name] = L.Convolution(bottom, kernel_size=ks, stride=stride,
                                    num_output=nout, pad=pad, weight_filler=dict(type='msra'), bias_term= bias_term)
        if "res" in name:
            self.net[name.replace("res", "bn")] = L.BatchNorm(self.net[name], in_place=True, batch_norm_param=dict(use_global_stats=self.deploy))
            self.net[name.replace("res", "scale")]  = L.Scale(self.net[name.replace("res", "bn")], in_place=True, scale_param=dict(bias_term=True))
            return self.net[name.replace("res", "scale")]
        else:
            self.net["bn_" + name] = L.BatchNorm(self.net[name], in_place=True, batch_norm_param=dict(use_global_stats=self.deploy))
            self.net["scale_" + name] = L.Scale(self.net["bn_" + name], in_place=True, scale_param=dict(bias_term=True))
            return self.net["scale_" + name]

    def rpn(self, bottom, gt_boxes, im_info, data):
        self.net["rpn_conv/3x3"] = L.Convolution(bottom, kernel_size=3, stride=1,
                                    num_output=512, pad=1,
                                    param= [{'lr_mult':1},{'lr_mult':2}],
                                    weight_filler=dict(type='gaussian', std=0.01),
                                    bias_filler=dict(type='constant', value=0))
        self.net["rpn_relu/3x3"] = L.ReLU(self.net["rpn_conv/3x3"] , in_place=True)
        self.net["rpn_cls_score"] = L.Convolution(self.net["rpn_relu/3x3"], kernel_size=1, stride=1,
                                    num_output= 2 * self.anchors, pad=0,
                                    param= [{'lr_mult':1},{'lr_mult':2}],
                                    weight_filler=dict(type='gaussian', std=0.01),
                                    bias_filler=dict(type='constant', value=0))
        self.net["rpn_bbox_pred"] = L.Convolution(self.net["rpn_relu/3x3"], kernel_size=1, stride=1,
                                    num_output= 4 * self.anchors, pad=0,
                                    param= [{'lr_mult':1},{'lr_mult':2}],
                                    weight_filler=dict(type='gaussian', std=0.01),
                                    bias_filler=dict(type='constant', value=0))
        self.net["rpn_cls_score_reshape"] = L.Reshape(self.net["rpn_cls_score"],
                                    reshape_param= {"shape" : { "dim": [0, 2, -1, 0]}})

        if not self.deploy:
            self.net["rpn_labels"], self.net["rpn_bbox_targets"], self.net["rpn_bbox_inside_weights"], self.net["rpn_bbox_outside_weights"] = \
                        L.Python(self.net["rpn_cls_score"], gt_boxes, im_info, data,
                            name = 'rpn-data',
                            python_param=dict(
                                            module='rpn.anchor_target_layer',
                                            layer='AnchorTargetLayer',
                                            param_str='"feat_stride": %s' %(self.feat_stride)),
                            ntop=4,)
            self.net["rpn_cls_loss"] = L.SoftmaxWithLoss(self.net["rpn_cls_score_reshape"], self.net["rpn_labels"], name = "rpn_loss_cls", propagate_down=[1,0],\
                            loss_weight = 1, loss_param = {"ignore_label": -1, "normalize": True})
            self.net["rpn_loss_bbox"] = L.SmoothL1Loss(self.net["rpn_bbox_pred"], self.net["rpn_bbox_targets"], \
                            self.net["rpn_bbox_inside_weights"], self.net["rpn_bbox_outside_weights"], \
                            name= "loss_bbox", loss_weight = 1, smooth_l1_loss_param = {"sigma": 3.0})
            return self.net["rpn_cls_loss"], self.net["rpn_loss_bbox"], self.net["rpn_cls_score_reshape"], self.net["rpn_bbox_pred"]
        else:
            return self.net["rpn_cls_score_reshape"], self.net["rpn_bbox_pred"]
        

    def roi_proposals(self, rpn_cls_score_reshape, rpn_bbox_pred, im_info, gt_boxes):
        self.net["rpn_cls_prob"] = L.Softmax(rpn_cls_score_reshape, name = "rpn_cls_prob")
        self.net["rpn_cls_prob_reshape"] = L.Reshape(self.net["rpn_cls_prob"], name = "rpn_cls_prob_reshape", \
                    reshape_param= {"shape" : { "dim": [0, 18, -1, 0]}})
        
        if not self.deploy:
            self.net["rpn_rois"] = L.Python(self.net["rpn_cls_prob_reshape"], rpn_bbox_pred, im_info, 
                            name = 'proposal',
                            python_param=dict(
                                            module='rpn.proposal_layer',
                                            layer='ProposalLayer',
                                            param_str='"feat_stride": %s' %(self.feat_stride)),
                            ntop=1,)
            self.net["rois"], self.net["labels"], self.net["bbox_targets"], self.net["bbox_inside_weights"], self.net["bbox_outside_weights"] = \
                        L.Python(self.net["rpn_rois"], self.net["gt_boxes"],
                            name = 'roi-data',
                            python_param=dict(
                                            module='rpn.proposal_target_layer',
                                            layer='ProposalTargetLayer',
                                            param_str='"num_classes": %s' %(self.classes)),
                            ntop=5,)
            return self.net["rois"], self.net["labels"], self.net["bbox_targets"], self.net["bbox_inside_weights"], self.net["bbox_outside_weights"]
        else:
            self.net["rois"] = L.Python(self.net["rpn_cls_prob_reshape"], rpn_bbox_pred, im_info, 
                            name = 'proposal',
                            python_param=dict(
                                            module='rpn.proposal_layer',
                                            layer='ProposalLayer',
                                            param_str='"feat_stride": %s' %(self.feat_stride)),
                            ntop=1,)
            return self.net["rois"]


    def final_cls_bbox(self, bottom):
        self.net["cls_score"] = L.InnerProduct(bottom, name = "cls_score",
                                num_output= self.classes,
                                param= [{'lr_mult':1},{'lr_mult':2}],
                                weight_filler=dict(type='gaussian', std=0.001),
                                bias_filler=dict(type='constant', value=0))
        self.net["bbox_pred"] = L.InnerProduct(bottom, name = "bbox_pred",
                                num_output= 4 * self.classes,
                                param= [{'lr_mult':1},{'lr_mult':2}],
                                weight_filler=dict(type='gaussian', std=0.001),
                                bias_filler=dict(type='constant', value=0))
        return self.net["cls_score"], self.net["bbox_pred"]

    def data_layer_train(self):
        if not self.deploy:
            self.net["data"], self.net["im_info"], self.net["gt_boxes"]= L.Python(
                                name = 'input-data',
                                python_param=dict(
                                                module='roi_data_layer.layer',
                                                layer='RoIDataLayer',
                                                param_str='"num_classes": %s' %(self.classes)),
                                ntop=3,)
        return self.net["data"], self.net["im_info"], self.net["gt_boxes"]

    def data_layer_test(self):
        if self.deploy:
            self.net["data"] = L.DummyData(shape=[dict(dim=[1, 3, 224, 224])])
            self.net["im_info"] = L.DummyData(shape=[dict(dim=[1, 3])])
        return self.net["data"], self.net["im_info"]

    def data_layer_train_with_ins(self):
        if not self.deploy:
            self.net["data"], self.net["im_info"], self.net["gt_boxes"], self.net["ins"] = L.Python(
                                name = 'input-data',
                                python_param=dict(
                                                module='roi_data_layer.layer',
                                                layer='RoIDataLayer',
                                                param_str='"num_classes": %s' %(self.classes)),
                                ntop=4,)
        return self.net["data"], self.net["im_info"], self.net["gt_boxes"], self.net["ins"]

    def pooling_layer(self, kernel_size, stride, pool_type, layer_name, bottom):
        self.net[layer_name] = L.Pooling(bottom, pool=eval("P.Pooling." + pool_type), kernel_size=kernel_size, stride=stride)
        return self.net[layer_name]

    def ave_pool(self, kernel_size, stride, layer_name, bottom):
        return self.pooling_layer(kernel_size, stride, 'AVE', layer_name, bottom)

    def residual_block_shortcut(self, name, bottom, num_filter, stride=1):
        conv1 = self.conv_factory(name + "_branch2a", bottom, 1, num_filter, stride, 0)
        conv2 = self.conv_factory(name + "_branch2b", conv1, 3, num_filter, stride, 1)
        conv3 = self.conv_factory_inverse_no_relu(name + "_branch2c", conv2, 1, 4 * num_filter, stride, 0)
        self.net[name] = L.Eltwise(bottom, conv3, operation=P.Eltwise.SUM)
        self.net[name + "_relu"] = L.ReLU(self.net[name], in_place=True)
        return self.net[name + "_relu"]

    def residual_block(self, name, bottom, num_filter, stride=1):
        conv1 = self.conv_factory(name + "_branch2a",bottom, 1, num_filter, stride, 0)
        conv2 = self.conv_factory(name + "_branch2b",conv1, 3, num_filter, 1, 1)
        conv3 = self.conv_factory_inverse_no_relu(name + "_branch2c", conv2, 1, 4 * num_filter, 1, 0)
        conv1_2 = self.conv_factory_inverse_no_relu(name + "_branch1", bottom, 1, 4 * num_filter, stride, 0)
        self.net[name] = L.Eltwise(conv3, conv1_2, operation=P.Eltwise.SUM)
        self.net[name + "_relu"] = L.ReLU(self.net[name], in_place=True)
        return self.net[name + "_relu"]

    def residual_block_shortcut_basic(self, name, bottom, num_filter, stride=1, deploy=False):
        conv1 = self.conv_factory(name + "_branch2b", bottom, 3, num_filter, stride, 1)
        conv2 = self.conv_factory_inverse_no_relu(name + "_branch2c", conv1, 3, 4 * num_filter, stride, 1)
        self.net[name] = L.Eltwise(bottom, conv2, name = name, operation=P.Eltwise.SUM)
        self.net[name + "_relu"] = L.ReLU(self.net[name], name = name + "_relu" , in_place=True)
        return self.net[name + "_relu"]
        
    def residual_block_basic(self, name, bottom, num_filter, stride=1, deploy=False):
        conv1 = self.conv_factory(name + "_branch2b",bottom, 3, num_filter, 1, 1, deploy)
        conv2 = self.conv_factory_inverse_no_relu(name + "_branch2c", conv1, 3, 4 * num_filter, 1, 0, deploy)
        conv1_2 = self.conv_factory_inverse_no_relu(name + "_branch1", bottom, 1, 4 * num_filter, stride, 0, deploy)
        self.net[name] = L.Eltwise(conv2, conv1_2, operation=P.Eltwise.SUM)
        self.net[name + "_relu"] = L.ReLU(self.net[name], name = name + "_relu" , in_place=True)
        return self.net[name + "_relu"]

    def resnet_rcnn(self):
        channals = self.channals
        data, im_info, gt_boxes = self.data_layer_train()
        conv1 = self.conv_factory("conv1", data, 7, channals, 2, 3, bias_term=True)
        pool1 = self.pooling_layer(3, 2, 'MAX', 'pool1', conv1)
        k=0
        index = 1
        out = pool1
        for i in self.stages[:-1]:
            index += 1
            for j in range(i):
                if j==0:
                    if index == 2:
                        stride = 1
                    else:
                        stride = 2  
                    if self.module == "normal":
                        out = self.residual_block("res" + str(index) + ascii_lowercase[j], out, channals, stride)
                    else:
                        out = self.residual_block_basic("res" + str(index) + ascii_lowercase[j], out, channals, stride)
                else:
                    if self.module == "normal":
                        out = self.residual_block_shortcut("res" + str(index) + ascii_lowercase[j], out, channals)
                    else:
                        out = self.residual_block_shortcut_basic("res" + str(index) + ascii_lowercase[j], out, channals)
            channals *= 2

        if not self.deploy:
            rpn_cls_loss, rpn_loss_bbox, rpn_cls_score_reshape, rpn_bbox_pred = self.rpn(out, gt_boxes, im_info, data)
            rois, labels, bbox_targets, bbox_inside_weights, bbox_outside_weights = \
                self.roi_proposals(rpn_cls_score_reshape, rpn_bbox_pred, im_info, gt_boxes)
        else:
            rpn_cls_score_reshape, rpn_bbox_pred = self.rpn(out, gt_boxes, im_info, data)
            rois = self.roi_proposals(rpn_cls_score_reshape, rpn_bbox_pred, im_info, gt_boxes)

        
        feat_aligned = self.roi_align(out, rois)
        out = feat_aligned

        index += 1
        for j in range(self.stages[-1]):
            if j==0:
                if index == 2:
                    stride = 1
                else:
                    stride = 2
                if self.module == "normal":
                    out = self.residual_block("res" + str(index) + ascii_lowercase[j], out, channals, stride)
                else:
                    out = self.residual_block_basic("res" + str(index) + ascii_lowercase[j], out, channals, stride)
            else:
                if self.module == "normal":
                    out = self.residual_block_shortcut("res" + str(index) + ascii_lowercase[j], out, channals)
                else:
                    out = self.residual_block_shortcut_basic("res" + str(index) + ascii_lowercase[j], out, channals)
        pool5 = self.ave_pool(7, 1, "pool5", out)
        cls_score, bbox_pred = self.final_cls_bbox(pool5)

        if not self.deploy:
            self.net["loss_cls"] = L.SoftmaxWithLoss(cls_score, labels, loss_weight= 1, propagate_down=[1,0])
            self.net["loss_bbox"] = L.SmoothL1Loss(bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights,\
                                loss_weight= 1)
        else:
            self.net["cls_prob"] =  L.Softmax(cls_score)
        return self.net.to_proto()

    def resnet_mask_rcnn(self):
        channals = self.channals
        if not self.deploy:
            data, im_info, gt_boxes, ins = self.data_layer_train_with_ins()
        else:
            data, im_info = self.data_layer_test()
            gt_boxes = None
        conv1 = self.conv_factory("conv1", data, 7, channals, 2, 3, bias_term=True)
        pool1 = self.pooling_layer(3, 2, 'MAX', 'pool1', conv1)
        k=0
        index = 1
        out = pool1
        for i in self.stages[:-1]:
            index += 1
            for j in range(i):
                if j==0:
                    if index == 2:
                        stride = 1
                    else:
                        stride = 2  
                    if self.module == "normal":
                        out = self.residual_block("res" + str(index) + ascii_lowercase[j], out, channals, stride)
                    else:
                        out = self.residual_block_basic("res" + str(index) + ascii_lowercase[j], out, channals, stride)
                else:
                    if self.module == "normal":
                        out = self.residual_block_shortcut("res" + str(index) + ascii_lowercase[j], out, channals)
                    else:
                        out = self.residual_block_shortcut_basic("res" + str(index) + ascii_lowercase[j], out, channals)
            channals *= 2

        if not self.deploy:
            rpn_cls_loss, rpn_loss_bbox, rpn_cls_score_reshape, rpn_bbox_pred = self.rpn(out, gt_boxes, im_info, data)
            rois, labels, bbox_targets, bbox_inside_weights, bbox_outside_weights = \
                self.roi_proposals(rpn_cls_score_reshape, rpn_bbox_pred, im_info, gt_boxes)
            ins_crop = L.Python(rois, ins,
                            name = 'ins_crop',
                            python_param=dict(
                                            module='crop_seg.layer',
                                            layer='CropSegLayer'),
                            ntop=1,)
        else:
            rpn_cls_score_reshape, rpn_bbox_pred = self.rpn(out, gt_boxes, im_info, data)
            rois = self.roi_proposals(rpn_cls_score_reshape, rpn_bbox_pred, im_info, gt_boxes)

        
        feat_aligned = self.roi_align(out, rois)
        out = feat_aligned

        index += 1
        for j in range(self.stages[-1]):
            if j==0:
                if index == 2:
                    stride = 1
                else:
                    stride = 2
                if self.module == "normal":
                    out = self.residual_block("res" + str(index) + ascii_lowercase[j], out, channals, stride)
                else:
                    out = self.residual_block_basic("res" + str(index) + ascii_lowercase[j], out, channals, stride)
            else:
                if self.module == "normal":
                    out = self.residual_block_shortcut("res" + str(index) + ascii_lowercase[j], out, channals)
                else:
                    out = self.residual_block_shortcut_basic("res" + str(index) + ascii_lowercase[j], out, channals)
        
        # for bbox detection
        pool5 = self.ave_pool(7, 1, "pool5", out)
        cls_score, bbox_pred = self.final_cls_bbox(pool5)

        if not self.deploy:
            self.net["loss_cls"] = L.SoftmaxWithLoss(cls_score, labels, loss_weight= 1, propagate_down=[1,0])
            self.net["loss_bbox"] = L.SmoothL1Loss(bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights,\
                                loss_weight= 1)
        else:
            self.net["cls_prob"] =  L.Softmax(cls_score)
        
        #for mask prediction
        mask_conv1 = self.conv_factory("mask_conv1", out, 3, 256, 1, 1, bias_term=True)
        mask_out = self.conv_factory("mask_out", mask_conv1, 1, 256, 1, 0, bias_term=True)
        if not self.deploy:
            self.net["loss_mask"] = L.SoftmaxWithLoss(mask_out, ins_crop, loss_weight= 1, propagate_down=[1,0])
        else:
            self.net["mask_prob"] =  L.Softmax(cls_score)

        return self.net.to_proto()

def main():
    resnet_test = ResNet(deploy=True)
    resnet_train = ResNet(deploy=False)
    #for net in ('18', '34', '50', '101', '152'):
    with open('test.prototxt', 'w') as f:
        f.write(str(resnet_test.resnet_mask_rcnn()))
    with open('train.prototxt', 'w') as f:
        f.write(str(resnet_train.resnet_mask_rcnn()))

if __name__ == '__main__':
    main()
