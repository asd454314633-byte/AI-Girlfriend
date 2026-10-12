###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
# 
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  
#       http://www.apache.org/licenses/LICENSE-2.0
# 
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################
#
#  Wav2Lip 数字人 — 迁移自 lipreal.py + lipasr.py
#

import math
import json
from threading import RLock
import torch
import numpy as np

import os
import time
import cv2
import glob
import pickle

import queue
from queue import Queue
from threading import Thread, Event
import torch.multiprocessing as mp

from avatars.audio_features.mel import MelASR
import asyncio
from av import AudioFrame, VideoFrame
from avatars.wav2lip.models import Wav2Lip
from avatars.base_avatar import BaseAvatar

from tqdm import tqdm
from utils.logger import logger
from utils.image import read_imgs, mirror_index
from utils.device import initialize_device
from registry import register

device = initialize_device()
logger.info('Using {} for inference.'.format(device))

def _load(checkpoint_path):
    if device == 'cuda':
        checkpoint = torch.load(checkpoint_path)
    else:
        checkpoint = torch.load(checkpoint_path,
                                map_location=lambda storage, loc: storage)
    return checkpoint

def load_model(path):
    model = Wav2Lip()
    logger.info("Load checkpoint from: {}".format(path))
    checkpoint = _load(path)
    s = checkpoint["state_dict"]
    new_s = {}
    for k, v in s.items():
        new_s[k.replace('module.', '')] = v
    model.load_state_dict(new_s)

    model = model.to(device)
    return model.eval()

def load_avatar(avatar_id):
    avatar_path = f"./data/avatars/{avatar_id}"
    full_imgs_path = f"{avatar_path}/full_imgs" 
    face_imgs_path = f"{avatar_path}/face_imgs" 
    coords_path = f"{avatar_path}/coords.pkl"
    
    with open(coords_path, 'rb') as f:
        coord_list_cycle = pickle.load(f)
    frame_list_cycle = None
    input_img_list = glob.glob(os.path.join(full_imgs_path, '*.[jpJP][pnPN]*[gG]'))
    input_img_list = sorted(input_img_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    frame_list_cycle = read_imgs(input_img_list)
    input_face_list = glob.glob(os.path.join(face_imgs_path, '*.[jpJP][pnPN]*[gG]'))
    input_face_list = sorted(input_face_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    face_list_cycle = read_imgs(input_face_list)

    return frame_list_cycle,face_list_cycle,coord_list_cycle

@torch.no_grad()
def warm_up(batch_size,model,modelres):
    # 预热函数
    logger.info('warmup model...')
    img_batch = torch.ones(batch_size, 6, modelres, modelres).to(device)
    mel_batch = torch.ones(batch_size, 1, 80, 16).to(device)
    model(mel_batch, img_batch)

# BEGIN UNMODIFIED R2 mouth.py
"""CPU color/connected-component mouth proxy, not trained landmarks or parsing.
Only used on the already generated 256x256 experimental face cache.
"""
import cv2,numpy as np
def locate(face):
    b,g,r=cv2.split(face.astype(np.float32)); hsv=cv2.cvtColor(face,cv2.COLOR_BGR2HSV)
    mask=((r>g*1.28)&(r>b*1.18)&(hsv[:,:,1]>65)).astype(np.uint8)
    mask[:155]=0;mask[224:]=0;mask[:,:65]=0;mask[:,190:]=0
    mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    cs,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    cs=[c for c in cs if cv2.contourArea(c)>35]
    if not cs:return {'box':[85,178,80,30],'valid':False,'opening_proxy':0.0}
    c=max(cs,key=cv2.contourArea);x,y,w,h=cv2.boundingRect(c)
    # Interior luminance holes can include shadow: report separately as a proxy.
    gray=cv2.cvtColor(face,cv2.COLOR_BGR2GRAY)
    interior=gray[y+max(2,h//4):y+max(3,3*h//4),x+w//4:x+3*w//4]
    dark=np.flatnonzero(np.any(interior<65,axis=1))
    gap=int(dark[-1]-dark[0]+1) if len(dark) else 0
    return {'box':list(map(int,(x,y,w,h))),'valid':True,'opening_proxy':gap/max(w,1),'lip_height_width':h/max(w,1)}

class AdaptiveBlend:
    def __init__(self):self.previous=None
    def mask(self,raw,base256):
        info=locate(raw);ref=locate(base256)
        x,y,w,h=info['box'];bx,by,bw,bh=ref['box']
        # Include both current source and generated lips, so a displaced source
        # mouth cannot leak through. Causal smoothing only changes feather support,
        # never delays/replaces the generated mouth pixels.
        left=min(x,bx);right=max(x+w,bx+bw);top=min(y,by);bottom=max(y+h,by+bh)
        target=np.array([(left+right)/2,(top+bottom)/2,right-left,bottom-top],np.float32)
        if self.previous is None:self.previous=target
        smooth=.65*target+.35*self.previous;self.previous=smooth
        cx,cy,ww,hh=smooth;ww=max(ww,right-left+abs(cx-(left+right)/2)*2);hh=max(hh,bottom-top+abs(cy-(top+bottom)/2)*2)
        yy,xx=np.mgrid[:256,:256].astype(np.float32)
        # Rectangle-distance rounded support: all lips and corners remain alpha=1.
        dx=np.maximum(np.abs(xx-cx)-(ww/2+5),0);dy=np.maximum(np.abs(yy-cy)-(hh/2+4),0)
        edge=np.sqrt((dx/17)**2+(dy/13)**2)
        mouth=np.clip(1-edge,0,1);mouth=mouth*mouth*(3-2*mouth)
        # Central chin participation expands with mouth height/opening; no frozen
        # full jaw. Outer cheeks/jaw silhouette remain reference, no pixel warping.
        opening=np.clip((h-18)/30,0,1)
        chin_r=np.sqrt(((xx-cx)/(ww*.48+5))**2+((yy-(bottom+10))/(16+opening*12))**2)
        chin=np.clip((1-chin_r)/.35,0,1)*(.35+.3*opening)
        alpha=np.maximum(mouth,chin)
        alpha*=np.clip((250-yy)/12,0,1)
        return alpha,{**info,'reference':ref,'opening_control':float(opening),'support_center':smooth.tolist()}

# END UNMODIFIED R2 mouth.py

@register("avatar", "wav2lip")
class LipReal(BaseAvatar):
    @torch.no_grad()
    def __init__(self, opt, model, avatar):
        super().__init__(opt)

        #self.fps = opt.fps # 20 ms per frame
        
        # self.batch_size = opt.batch_size
        # self.idx = 0
        # self.res_frame_queue = Queue(self.batch_size*2)
        self.model = model

        self.frame_list_cycle,self.face_list_cycle,self.coord_list_cycle = avatar

        self._init_lip_mode()
        self.asr = MelASR(opt,self)
        self.asr.warm_up()
    
    def inference_batch(self, index, audiofeat_batch):
        # 这里的 index 是针对当前 avatar 的索引
        # 返回一个 batch 的推理结果，batch 大小由 self.batch_size 决定
        length = len(self.face_list_cycle)
        img_batch = []
        for i in range(self.batch_size):
            idx = mirror_index(length, index + i)
            face = self.face_list_cycle[idx]
            img_batch.append(face)
        img_batch, audiofeat_batch = np.asarray(img_batch), np.asarray(audiofeat_batch)

        img_masked = img_batch.copy()
        img_masked[:, face.shape[0]//2:] = 0

        img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
        audiofeat_batch = np.reshape(audiofeat_batch, [len(audiofeat_batch), audiofeat_batch.shape[1], audiofeat_batch.shape[2], 1])
        
        img_batch = torch.FloatTensor(np.transpose(img_batch, (0, 3, 1, 2))).to(device)
        audiofeat_batch = torch.FloatTensor(np.transpose(audiofeat_batch, (0, 3, 1, 2))).to(device)

        with torch.no_grad():
            pred = self.model(audiofeat_batch, img_batch)
        pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.
        return pred

    def _init_lip_mode(self):
        self._lip_lock = RLock()
        self._lip_requested = 'ORIGINAL'
        self._lip_applied = 'ORIGINAL'
        self._lip_effective = 'ORIGINAL'
        self._lip_ready = False
        self._lip_pending = False
        self._lip_applied_revision = 0
        self._lip_failed = False
        self._lip_reason = None
        self._lip_tracker = None
        self._lip_frame = None
        self._lip_revision = 0
        self._lip_render_revision = None

    def _lip_snapshot(self):
        return dict(sessionid=str(self.sessionid), requested_mode=self._lip_requested,
                    actual_mode=self._lip_effective, applied_mode=self._lip_applied,
                    pending=self._lip_pending,
                    fallback_reason=self._lip_reason, last_composed_frame=self._lip_frame,
                    request_revision=self._lip_revision, rendered_revision=self._lip_render_revision,
                    awaiting_first_frame=self._lip_render_revision != self._lip_revision,
                    switch_boundary='observed_idle_then_next_speech_frame')

    def lipsync_mode_status(self):
        with self._lip_lock:
            # BaseAvatar is the authoritative producer of speaking=False.
            # Only arm the switch during an observed idle state; never drain audio.
            if not self.speaking and self._lip_pending:
                self._lip_ready = True
            return self._lip_snapshot()

    def request_lipsync_mode(self, mode):
        if mode not in ('ORIGINAL', 'D_EXPERIMENTAL'):
            raise ValueError('Unsupported lipsync mode')
        with self._lip_lock:
            self._lip_requested = mode
            self._lip_revision += 1
            # An explicit request is the only way to retry a latched D failure.
            self._lip_pending = True
            self._lip_ready = not self.speaking
            logger.info('lipsync_mode_request %s', json.dumps(self._lip_snapshot()))
            return self._lip_snapshot()

    def paste_back_frame(self,pred_frame,idx:int):
        # Exact original path is constructed before attempting D. Model, idx,
        # audio, queue, timestamps and cache data are never changed here.
        bbox = self.coord_list_cycle[idx]
        combine_frame = self.frame_list_cycle[idx].copy()
        y1, y2, x1, x2 = bbox
        raw = pred_frame.astype(np.uint8)
        res_frame = cv2.resize(raw,(x2-x1,y2-y1))
        combine_frame[y1:y2, x1:x2] = res_frame
        with self._lip_lock:
            if self._lip_ready:
                self._lip_applied = self._lip_requested
                self._lip_ready = False
                self._lip_pending = False
                self._lip_applied_revision = self._lip_revision
                self._lip_failed = False
                self._lip_reason = None
                self._lip_tracker = None
            result = combine_frame
            effective = 'ORIGINAL'
            if self._lip_applied == 'D_EXPERIMENTAL' and not self._lip_failed:
                try:
                    ref = self.face_list_cycle[idx]
                    if raw.shape != (256,256,3) or ref.shape != (256,256,3):
                        raise ValueError('D requires matched 256x256 BGR faces')
                    if self._lip_tracker is None:
                        self._lip_tracker = AdaptiveBlend()
                    alpha, info = self._lip_tracker.mask(raw,ref)
                    if alpha.shape != (256,256) or not np.isfinite(alpha).all():
                        raise ValueError('D returned invalid alpha')
                    ad = cv2.resize(alpha,(x2-x1,y2-y1))
                    base = self.frame_list_cycle[idx]
                    result = base.copy()
                    result[y1:y2,x1:x2] = (ad[...,None]*res_frame +
                        (1-ad[...,None])*base[y1:y2,x1:x2]).astype(np.uint8)
                    effective = 'D_EXPERIMENTAL'
                except Exception as exc:
                    self._lip_failed = True
                    self._lip_tracker = None
                    self._lip_reason = type(exc).__name__ + ': ' + str(exc)[:300]
                    result = combine_frame
            changed = (effective != self._lip_effective or
                       self._lip_render_revision != self._lip_applied_revision)
            self._lip_effective = effective
            self._lip_frame = int(idx)
            self._lip_render_revision = self._lip_applied_revision
            if changed or self._lip_failed and not getattr(self, '_lip_failure_logged', False):
                logger.info('lipsync_mode_frame %s', json.dumps(self._lip_snapshot()))
            self._lip_failure_logged = self._lip_failed
            return result
