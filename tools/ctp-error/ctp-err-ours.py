# coding=utf-8
import argparse
import os
import sys

sys.path.append('/home/dell/yifeis/symnet/')
import math
import random
import time
import torch
import torch.nn.parallel
import torch.utils.data
from torch.autograd import Variable
from datasets.baseline.dataset_eval import PoseDataset as PoseDataset_ycb
from lib.network import SymNet
import matplotlib.pyplot as plt
import numpy as np
import open3d

import sklearn.cluster as skc

parser = argparse.ArgumentParser()
parser.add_argument('--dataset', type=str, default = 'shapenet', help='shapenet or scan2cad')
parser.add_argument('--dataset_root', type=str, default = '', help='dataset root dir')
parser.add_argument('--batch_size', type=int, default = 8, help='batch size')
parser.add_argument('--workers', type=int, default = 0, help='number of data loading workers')
parser.add_argument('--noise_trans', default=0.03, help='range of the random noise of translation added to the training data')
parser.add_argument('--nepoch', type=int, default=500, help='max number of epochs to train')
parser.add_argument('--resume_posenet', type=str, default = '',  help='resume PoseNet model')
opt = parser.parse_args()

proj_dir = '/home/dell/yifeis/symnet/'
model_dir = '/home/dell/yifeis/pose_estimation/densefusion_syn_test/'
device_ids = [0]
os.environ["CUDA_VISIBLE_DEVICES"] = "0"


def prcurve(DIST_THRESHOLD):
    opt.manualSeed = random.randint(1, 10000)
    random.seed(opt.manualSeed)
    torch.manual_seed(opt.manualSeed)

    if opt.dataset == 'ycb':
        opt.num_objects = 21  # number of object classes in the dataset
        opt.num_points = 1000  # number of points on the input pointcloud
        opt.outf = model_dir + 'trained_models/ycb'  # folder to save trained models
        opt.log_dir = proj_dir + 'visualization'  # folder to save logs
        opt.repeat_epoch = 1  # number of repeat times for one epoch training

    estimator = torch.nn.DataParallel(SymNet(num_points=opt.num_points))
    # torch.nn.DataParallel(module=estimator, device_ids=device_ids)
    estimator.cuda(device_ids[0])
    torch.nn.DataParallel(module=estimator, device_ids=device_ids)

    if opt.resume_posenet != '':
        estimator.load_state_dict(torch.load('{0}/{1}'.format(opt.outf, opt.resume_posenet)))
        estimator = estimator.module
        opt.refine_start = False
        opt.decay_start = False

    else:
        opt.refine_start = False
        opt.decay_start = False
        opt.w *= opt.w_rate

    if opt.dataset == 'ycb':
        dataset = PoseDataset_ycb('syn_train', opt.num_points, False, opt.dataset_root, opt.noise_trans,
                                  opt.refine_start)
        test_dataset = PoseDataset_ycb('syn_frame', opt.num_points, False, opt.dataset_root, 0.0, opt.refine_start)
    testdataloader = torch.utils.data.DataLoader(test_dataset, batch_size=1, shuffle=False, num_workers=opt.workers)
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=1, shuffle=False, num_workers=opt.workers)

    opt.sym_list = dataset.get_sym_list()
    opt.num_points_mesh = dataset.get_num_points_mesh()

    print(
        '>>>>>>>>----------Dataset loaded!---------<<<<<<<<\nlength of the training set: {0}\nlength of the testing set: {1}\nnumber of sample points on mesh: {2}\nsymmetry object list: {3}'.format(
            len(dataset), len(test_dataset), opt.num_points_mesh, opt.sym_list))

    estimator.eval()
    total_fream = 0
    total_num = 0
    ctp_dis_list = []

    for j, data in enumerate(testdataloader, 0):
        points, choose, img, idx, target_s, target_num, target_mode, occ, cloud_, depth, cam_ins, num_pt, _ = data

        points, choose, img, idx, target_s, target_num, target_mode = Variable(points).cuda(), \
                                                                      Variable(choose).cuda(), \
                                                                      Variable(img).cuda(), \
                                                                      Variable(idx).cuda(), \
                                                                      Variable(target_s).cuda(), \
                                                                      Variable(target_num).cuda(), \
                                                                      Variable(target_mode).cuda()

        pred_cent, pred_ref, pred_foot_ref, pred_rot, pred_num, pred_mode, emb = estimator(img, points,
                                                                                           choose)
        target_mode = target_mode.data.cpu().numpy().reshape(-1)
        occ = occ.data.cpu().numpy()[0]
        #  ((occ < 0.7)or(occ>0.8)) : 70-80
        #  ((occ <0.6)or(occ>0.7)) :  60-70
        #  (occ>0.6) :  <60
        if (target_mode == 1) :
            continue

        total_fream += 1
        tmp_s = target_s.data.cpu().numpy()
        tmp_s = tmp_s.reshape(-1, 3)
        target_cent = tmp_s[0, :]
        target_sym = tmp_s[1:, :]  # target symmetry point
        len_target_sym = np.linalg.norm((target_sym - target_cent), axis=1)
        num_pt = num_pt.data.cpu().numpy()[0]
        cam_ins = cam_ins.data.cpu().numpy().reshape(4)
        depth = depth.data.cpu().numpy()

        points = points.view(1000, 3)
        points = points.detach().cpu().data.numpy()

        pred_cent = pred_cent.detach().cpu().data.numpy()
        pred_cent = pred_cent.reshape(1000, 3)
        cent_pred = (points + pred_cent)

        pred_num = pred_num.view(1000, 3).detach()
        my_num = torch.mean(pred_num, dim=0)
        my_num = my_num.data.cpu().numpy()

        pred_ref = pred_ref.detach().cpu().data.numpy()  # (1,1000,9)
        pred_ref = pred_ref.reshape(1000, -1, 3)

        pred_foot_ref = pred_foot_ref.view(1000, -1, 3)
        pred_foot_ref = pred_foot_ref.detach().cpu().data.numpy()

        target_sym = target_sym - target_cent
        # target_sym = target_sym.reshape(-1,3)

        my_sym = pred_ref
        my_norm = np.zeros(my_sym.shape)
        for i in range(my_sym.shape[1]):
            for k in range(3):
                my_norm[:, i, k] = my_sym[:, i, k] / np.linalg.norm(my_sym[:, i, :], axis=1)

        mean_norm = np.mean(my_norm, axis=0)  # n*3
        mean_cent = np.mean(cent_pred, axis=0)  # 1*3
        out_cent = mean_cent

        # ########DBSCAN
        st_time = time.time()
        print('st_time = ', st_time)
        out_sym = np.zeros(mean_norm.shape)
        sym_conf = np.zeros(mean_norm.shape[0])
        norm_conf_list = np.zeros(mean_norm.shape[0])
        for i in range(my_norm.shape[1]):
            this_norm = my_norm[:, i, :].reshape(1000, 3)
            dim_conf = 0
            for t in range(3):
                this_dim = this_norm[:, t].reshape(1000, 1)
                # target_dim = target_sym[i,j]
                mean_dim = np.mean(this_dim, axis=0)
                db = skc.DBSCAN(eps=0.2, min_samples=500).fit(this_dim)  # DBSCAN聚类方法 还有参数，matric = ""距离计算方法
                labels = db.labels_  # 和X同一个维度，labels对应索引序号的值 为她所在簇的序号。若簇编号为-1，表示为噪声
                clster_center = np.mean(this_dim[labels[:] == 0], axis=0)
                out_sym[i, t] = clster_center
            #     dim_conf += len(labels[labels[:] == 0]) / len(labels)
            # norm_conf = dim_conf/3
            if np.isnan(out_sym[i]).any():
                norm_conf = 0
            else:
                norm_conf = 1
            norm_conf_list[i] = norm_conf
            # sym_conf[i] = my_num[i] * norm_conf
        edtime = time.time() - st_time
        print('endtime=', edtime)
        #######RANSAC
        # out_cent = mean_cent
        # out_sym = ransac_syms
        # sef_conf = my_num

        ######refinement
        # refine_sym = symmetry_icp_refinement(points, out_sym_, mean_cent, 0.005)
        # out_cent = refine_sym[:3]
        # out_sym = refine_sym[3:].reshape(-1, 3)
        out_sym = np.nan_to_num(out_sym)
        ########self-loss
        self_conf_list = []
        for i in range(3):
            outlier = self_loss(out_sym[i], out_cent, points, depth, cam_ins, 100)
            self_conf = 1 - outlier / 1000
            self_conf_list.append(self_conf)
            sym_conf[i] = my_num[i] *self_conf *norm_conf_list[i]

        my_ref = reflect(points, out_cent, out_sym)
        target_ref = reflect(points, target_cent, target_sym)

        # angle_list = []
        # maxid = np.argmax(my_num)
        # # for i in range(out_sym.shape[0]):
        # pred_product = np.abs(np.matmul(out_sym[maxid], target_sym[0].T) / (
        #         np.linalg.norm(target_sym[0]) * np.linalg.norm(out_sym[0])))
        # angle_error = math.acos(pred_product) / math.pi * 180
        # dis_error = np.abs((target_sym[0, :] * (out_cent - target_cent)).sum())
        # angle_list.append(angle_error)

        target_sym = target_sym.reshape(-1, 3)
        # counterpart = np.zeros(pred_ref.shape)
        target_vector = target_ref - points.reshape(1000, 1, 3).repeat(target_ref.shape[1], axis=1)
        # for i in range(3):
        counterpart = pred_ref + points.reshape(1000, 1, 3).repeat(3, axis=1)

        rst_list = []

        for m in range(out_sym.shape[0]):
            if sym_conf[m]<0.3:
                continue
            for n in range(target_sym.reshape(-1, 3).shape[0]):
                target_len = np.linalg.norm(target_vector[:, n, :], axis=1)
                max_len = np.max(target_len)
                dense_dis = np.linalg.norm((my_ref[:, m, :] - target_ref[:, n, :]), axis=1)
                ctp_dis = np.linalg.norm((counterpart[:, m, :] - target_ref[:, n, :]), axis=1)
                dis_mean = np.mean(ctp_dis, axis=0)
                metric_mean = dis_mean / max_len
                rst_list.append(metric_mean)
            ctp_dis_list.append(np.min(rst_list))
            total_num+=1

        print('predcting frame:', j, ' object:', idx, 'target_num', target_num)

    thresh_list = []
    indis_list = []
    ctp_dis_list = np.array(ctp_dis_list)

    # np.savetxt('./ctp_veri_dis_list-ins-0.5-max.txt', ctp_dis_list)
    for t in range(1, 50001):
        dis_thresh = t / 10000
        indis = len(np.where(ctp_dis_list <= dis_thresh)[0])
        indis_list.append(indis / total_num)
        thresh_list.append(dis_thresh)
    return indis_list, thresh_list


def augment(xyzs):
    axyz = np.ones((len(xyzs), 4))
    axyz[:, :3] = xyzs
    return axyz


def estimate(xyzs):
    axyz = augment(xyzs[:3])
    return np.linalg.svd(axyz)[-1][-1, :]


def is_inlier(coeffs, xyz, threshold):
    return np.abs(coeffs.dot(augment([xyz]).T)) < threshold


def run_ransac(data, estimate, is_inlier, sample_size, goal_inliers, max_iterations, stop_at_goal=True,
               random_seed=None):
    best_ic = 0
    best_model = None
    random.seed(random_seed)
    # random.sample cannot deal with "data" being a numpy array
    data = list(data)
    for i in range(max_iterations):
        s = random.sample(data, int(sample_size))
        m = estimate(s)
        ic = 0
        for j in range(len(data)):
            if is_inlier(m, data[j]):
                ic += 1

        print(s)
        print('estimate:', m, )
        print('# inliers:', ic)

        if ic > best_ic:
            best_ic = ic
            best_model = m
            if ic > goal_inliers and stop_at_goal:
                break
    print('took iterations:', i + 1, 'best model:', best_model, 'explains:', best_ic)
    return best_model, best_ic


def reflect(Data, cent, sym):
    Data = Data.reshape(1000, 3)
    cent = cent.reshape(3)
    sym = sym.reshape(-1, 3)
    reflect_points = np.zeros((1000, sym.shape[0], 3))
    for j in range(sym.shape[0]):
        x1 = cent[0]  # 对称面上中心点
        y1 = cent[1]
        z1 = cent[2]
        a = sym[j, 0]  # 对称面法向量
        b = sym[j, 1]
        c = sym[j, 2]
        ref_point = np.zeros(Data.shape)
        for i in range(0, Data.shape[0]):
            d = a * x1 + b * y1 + c * z1
            t = (d - (a * Data[i][0] + b * Data[i][1] + c * Data[i][2])) / (a * a + b * b + c * c)
            sym_x = 2 * a * t + Data[i][0]  ##计算得到的对称点
            sym_y = 2 * b * t + Data[i][1]
            sym_z = 2 * c * t + Data[i][2]
            ref_point[i, :] = np.array([sym_x, sym_y, sym_z])
        reflect_points[:, j, :] = ref_point
    return reflect_points

def symmetry_icp_refinement(visual_points, pred_sym, pred_cent, icp_threshold):
    refined_pred_sym = []
    source = open3d.geometry.PointCloud()
    target = open3d.geometry.PointCloud()
    target_trans = open3d.geometry.PointCloud()
    source.points = open3d.utility.Vector3dVector(visual_points)
    source_colors = np.zeros(visual_points.shape)
    for i in range(source_colors.shape[0]):
        source_colors[i, 0] = 0.2
        source_colors[i, 1] = 0.2
        source_colors[i, 2] = 0.2
    source.colors = open3d.utility.Vector3dVector(source_colors)

    cent = pred_cent.reshape(3)
    sym = pred_sym
    sym = sym.reshape(-1, 3)

    for j in range(sym.shape[0]):
        x1 = cent[0]
        y1 = cent[1]
        z1 = cent[2]
        a = sym[j, 0]
        b = sym[j, 1]
        c = sym[j, 2]
        x2 = cent[0] + sym[j, 0]
        y2 = cent[1] + sym[j, 1]
        z2 = cent[2] + sym[j, 2]
        ref_points = np.zeros(visual_points.shape)
        for i in range(0, visual_points.shape[0]):
            d = a * x1 + b * y1 + c * z1
            t = (d - (a * visual_points[i][0] + b * visual_points[i][1] + c * visual_points[i][2])) / (
                    a * a + b * b + c * c)
            sym_x = 2 * a * t + visual_points[i][0]
            sym_y = 2 * b * t + visual_points[i][1]
            sym_z = 2 * c * t + visual_points[i][2]
            ref_points[i, :] = np.array([sym_x, sym_y, sym_z])

        target.points = open3d.utility.Vector3dVector(ref_points)
        ref_colors = np.zeros(ref_points.shape)
        for i in range(ref_colors.shape[0]):
            ref_colors[i, 0] = 1
            ref_colors[i, 1] = 0.5
            ref_colors[i, 2] = 0.5
        target.colors = open3d.utility.Vector3dVector(ref_colors)

        source.estimate_normals(search_param=open3d.geometry.KDTreeSearchParamHybrid(
            radius=0.1, max_nn=30))
        target.estimate_normals(search_param=open3d.geometry.KDTreeSearchParamHybrid(
            radius=0.1, max_nn=30))
        trans_init = np.asarray([[1.0, 0.0, 0.0, 0.0],
                                 [0.0, 1.0, 0.0, 0.0],
                                 [0.0, 0.0, 1.0, 0.0],
                                 [0.0, 0.0, 0.0, 1.0]])
        reg_p2l = open3d.registration.registration_icp(
            target, source, icp_threshold, trans_init,
            open3d.registration.TransformationEstimationPointToPlane())
        print(reg_p2l)

        points = np.asarray(target.points)
        ones = np.ones((points.shape[0], 1))
        points = np.hstack((points, ones))
        points = np.dot(reg_p2l.transformation, points.T).T[:, :3]

        target_trans.points = open3d.utility.Vector3dVector(points)
        target_trans_colors = np.zeros(points.shape)
        for i in range(target_trans_colors.shape[0]):
            target_trans_colors[i, 0] = 0.5
            target_trans_colors[i, 1] = 1
            target_trans_colors[i, 2] = 0.5
        target_trans.colors = open3d.utility.Vector3dVector(target_trans_colors)

        p1 = np.array([x1, y1, z1, 1])
        p2 = np.array([x2, y2, z2, 1])
        p1 = np.dot(reg_p2l.transformation, p1)
        p2 = np.dot(reg_p2l.transformation, p2)
        cent_new = p1
        sym_new = p2 - p1
        # draw points here
        # open3d.visualization.draw_geometries([source,target,target_trans])

        if j == 0:
            refined_pred_sym.append(cent_new[0])
            refined_pred_sym.append(cent_new[1])
            refined_pred_sym.append(cent_new[2])
            refined_pred_sym.append(sym_new[0])
            refined_pred_sym.append(sym_new[1])
            refined_pred_sym.append(sym_new[2])
        else:
            refined_pred_sym.append(sym_new[0])
            refined_pred_sym.append(sym_new[1])
            refined_pred_sym.append(sym_new[2])

    refined_pred_sym = np.array(refined_pred_sym)
    print('pred_sym', pred_sym)
    print('refined_pred_sym', refined_pred_sym)
    return refined_pred_sym
def self_loss(sym,center, input_point, rgbd, instrin, depth_scale):
    Data = input_point.reshape(1000,3)     ##read point n*6
    unsupport_count=0
    center = center.reshape(3)
    sym = sym.reshape(3)
    x1 = center[0]                  #对称面上中心点
    y1 = center[1]
    z1 = center[2]
    a = sym[0]                   #对称面法向量
    b = sym[1]
    c = sym[2]
    depth_image = rgbd[0]  ##depth image
    camera_cx = instrin[0]
    camera_cy = instrin[1]
    camera_fx = instrin[2]
    camera_fy = instrin[3]
    for i in range(0, Data.shape[0]):
        d = a*x1+b*y1+c*z1
        t = (d-(a*Data[i][0]+b*Data[i][1]+c*Data[i][2]))/(a*a+b*b+c*c)
        sym_x = 2 * a * t + Data[i][0]                      ##计算得到的对称点
        sym_y = 2 * b * t + Data[i][1]
        sym_z = 2 * c * t + Data[i][2]
        n_pre = ((sym_x) * camera_fx) / sym_z + camera_cx   ##depth index
        m_pre = ((sym_y) * camera_fy) / sym_z + camera_cy
        x_pre = m_pre
        y_pre = n_pre
        if x_pre >= 540 or x_pre < 0 or y_pre < 0 or y_pre >= 960 or math.isnan(y_pre) or math.isnan(x_pre):  ##防止深度图索引越界
            unsupport_count = unsupport_count+1
        else:
            x_pre = int(x_pre)
            y_pre = int(y_pre)
            ori_depth = depth_image[x_pre][y_pre]   ##depth value on surface
            pre_depth = sym_z*depth_scale           ##depth value of symmetric point
            if ori_depth - pre_depth > 30:         ##相差大于2.5cm计数
                unsupport_count = unsupport_count+1
    return unsupport_count

if __name__ == '__main__':
    st_time = time.time()
    savedir = proj_dir + 'tools/ctp-error/'
    ang_list = [5, 10, 15, 20, 25]
    dis_list = [5, 10, 15, 20, 25]
    distance_list = [0.05, 0.1, 0.3, 0.4, 0.5]
    DIST_THRESHOLD = 15
    plt.style.use('fivethirtyeight')

    i = 0
    disin_list, thresh_list = prcurve(math.tan(DIST_THRESHOLD / 180 * math.pi))
    plt.plot(thresh_list, disin_list, linewidth=3)

    plot_data = np.concatenate((np.array(thresh_list).reshape(-1, 1), np.array(disin_list).reshape(-1, 1)), axis=1)
    np.savetxt(savedir + 'eval-veri-frame-0.3-max' + '.txt', plot_data)

    end_time = time.time()
    print("run_time=", end_time - st_time)
    plt.axis([0, 1, 0, 1])
    plt.legend(loc='upper right', fontsize=15)
    plt.xlabel('% counterpart error', fontsize=20)
    plt.ylabel('correspondences', fontsize=20)
    plt.tick_params(axis='both', labelsize=15)
    plt.title('counterpart eval\n', fontsize=20)
    plt.savefig(savedir + 'ctp-veri-frame-0.3-max.png', dpi=300, bbox_inches='tight')
