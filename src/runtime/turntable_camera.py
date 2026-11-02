"""Photo-estimated turntable camera. Explicitly not a physical calibration.

No object class, expected shape, mask filling or view deletion is used.
The legacy hardware remains orthographic; this module is a PS hypothesis.
"""
import numpy as np
import itertools

VERSION=43


def optical_flow_tracks(frames565,masks,cv2):
    """Older-OpenCV alternative: bidirectional subpixel tracks, no new frames."""
    grays=[];regions=[]
    for a,mask in zip(frames565,masks):
        r,g,b=(a>>11)&31,(a>>5)&63,a&31
        rgb=np.stack(((r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)),axis=-1).astype(np.uint8)
        grays.append(cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY));regions.append(cv2.erode(np.asarray(mask,np.uint8)*255,np.ones((5,5),np.uint8)))
    tracks=[];active=[];ids=[]
    for i,gray in enumerate(grays):
        if i and len(active):
            prev=np.asarray(active,np.float32).reshape(-1,1,2)
            current,status,_=cv2.calcOpticalFlowPyrLK(grays[i-1],gray,prev,None,winSize=(21,21),maxLevel=3,
                criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,30,.01))
            if current is None:active=[];ids=[]
            else:
                back,reverse,_=cv2.calcOpticalFlowPyrLK(gray,grays[i-1],current,None,winSize=(21,21),maxLevel=3,
                    criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,30,.01))
                good=np.zeros(len(prev),bool)
                if status is not None and back is not None and reverse is not None:
                    good=(status.ravel()!=0)&(reverse.ravel()!=0)&(np.linalg.norm(back[:,0]-prev[:,0],axis=1)<.75)
                legal=np.flatnonzero(good)
                if len(legal)>=12:
                    p=prev[legal,0];q=current[legal,0]
                    if hasattr(cv2,'USAC_MAGSAC'):
                        _,epipolar=cv2.findFundamentalMat(p,q,cv2.USAC_MAGSAC,1.5,.999,10000)
                    else:
                        _,epipolar=cv2.findFundamentalMat(p,q,cv2.FM_RANSAC,1.5,.999)
                    if epipolar is None:good[:]=False
                    else:good[legal]&=epipolar.ravel()!=0
                else:good[:]=False
                new_active=[];new_ids=[]
                for k,(x,y) in enumerate(current[:,0]):
                    if not good[k] or not 5<=x<gray.shape[1]-5 or not 5<=y<gray.shape[0]-5 or not regions[i][int(round(y)),int(round(x))]:continue
                    a=cv2.getRectSubPix(grays[i-1],(7,7),tuple(prev[k,0])).astype(float);b=cv2.getRectSubPix(gray,(7,7),(float(x),float(y))).astype(float)
                    a-=a.mean();b-=b.mean();den=np.sqrt((a*a).sum()*(b*b).sum())
                    if den<1 or float((a*b).sum()/den)<.9:continue
                    new_active.append((float(x),float(y)));new_ids.append(ids[k]);tracks[ids[k]].append([i,float(x),float(y)])
                active=new_active;ids=new_ids
        mask=regions[i].copy()
        for x,y in active:cv2.circle(mask,(int(round(x)),int(round(y))),7,0,-1)
        points=cv2.goodFeaturesToTrack(gray,maxCorners=max(1,240-len(active)),qualityLevel=.01,minDistance=7,mask=mask,blockSize=5)
        if points is not None:
            for x,y in points[:,0]:
                active.append((float(x),float(y)));ids.append(len(tracks));tracks.append([[i,float(x),float(y)]])
    tracks=[np.asarray(t,float) for t in tracks if len(t)>=4 and np.ptp(np.asarray(t)[:,1])>=8]
    return sorted(tracks,key=len,reverse=True)[:64],dict(available=True,backend='bidirectional_LK',tracks=min(64,len(tracks)),images_modified=False)


def photo_tracks(frames565,masks,backend_override=None):
    """Optional accelerated image matching, never a replacement PL segmenter."""
    try:
        import cv2
    except ImportError:
        return [],dict(available=False,reason='opencv_not_available_camera_not_estimated')
    cv2.setNumThreads(1)
    backend='SIFT'
    if backend_override=='LK' or not hasattr(cv2,'SIFT_create'):
        return optical_flow_tracks(frames565,masks,cv2)
    detector=cv2.SIFT_create(nfeatures=2500,contrastThreshold=.015,edgeThreshold=15)
    keys=[];descriptors=[]
    for packed,mask in zip(frames565,masks):
        a=np.asarray(packed);r,g,b=(a>>11)&31,(a>>5)&63,a&31
        rgb=np.stack(((r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)),axis=-1).astype(np.uint8)
        region=cv2.erode(np.asarray(mask,np.uint8)*255,np.ones((5,5),np.uint8))
        key,desc=detector.detectAndCompute(cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY),region);keys.append(key);descriptors.append(desc)
    matcher=cv2.BFMatcher(cv2.NORM_L2);links=[];count=len(keys)
    for i in range(count):
        for gap in (1,2):
            j=(i+gap)%count
            if descriptors[i] is None or descriptors[j] is None or min(len(descriptors[i]),len(descriptors[j]))<2:continue
            fw=matcher.knnMatch(descriptors[i],descriptors[j],k=2);bw=matcher.knnMatch(descriptors[j],descriptors[i],k=2)
            reverse={pair[0].queryIdx:pair[0].trainIdx for pair in bw if len(pair)==2 and pair[0].distance<.75*pair[1].distance}
            pairs=[(pair[0].queryIdx,pair[0].trainIdx) for pair in fw if len(pair)==2 and pair[0].distance<.75*pair[1].distance and reverse.get(pair[0].trainIdx)==pair[0].queryIdx]
            if len(pairs)<10:continue
            p=np.float32([keys[i][x].pt for x,y in pairs]);q=np.float32([keys[j][y].pt for x,y in pairs])
            if hasattr(cv2,'USAC_MAGSAC'):
                _,ok=cv2.findFundamentalMat(p,q,cv2.USAC_MAGSAC,1.5,.999,10000)
            else:
                _,ok=cv2.findFundamentalMat(p,q,cv2.FM_RANSAC,1.5,.999)
            if ok is not None:links.extend(((i,x),(j,y)) for (x,y),keep in zip(pairs,ok.ravel()) if keep)
    parent={};views={}
    def find(x):
        if x not in parent:parent[x]=x;views[x]={x[0]}
        if parent[x]!=x:parent[x]=find(parent[x])
        return parent[x]
    for x,y in links:
        a,b=find(x),find(y)
        if a!=b and not views[a].intersection(views[b]):parent[b]=a;views[a]|=views[b]
    groups={}
    for k in parent:groups.setdefault(find(k),[]).append(k)
    tracks=[]
    for group in groups.values():
        if len(group)<4:continue
        p=np.asarray([[i,keys[i][k].pt[0],keys[i][k].pt[1]] for i,k in sorted(group)],float)
        if np.ptp(p[:,1])>=8:tracks.append(p)
    tracks=sorted(tracks,key=len,reverse=True)[:64]
    return tracks,dict(available=True,backend=backend,opencv_version=str(cv2.__version__),
        feature_counts=[len(k) for k in keys],mutual_epipolar_links=len(links),tracks=len(tracks),
        method='mutual_ratio_bidirectional_epipolar_track_matching',images_modified=False)


def _extremum_track(masks,angles):
    from pl_mask import _regions
    points=[]
    for mask in np.asarray(masks,bool):
        runs,labels,areas,_=_regions(mask,mask)
        if not len(labels):return None
        primary=int(max(np.unique(labels),key=lambda k:areas[k]));rr=[(y,a,b) for y,a,b,node in runs if labels[node]==primary]
        top=min(y for y,a,b in rr);xs=[x for y,a,b in rr if y==top for x in range(a,b)]
        points.append([float(np.mean(xs)),float(top)])
    xy=np.asarray(points);a=np.deg2rad(angles);d=np.stack([np.ones(len(a)),np.cos(a),np.sin(a)],1);best=None;needed=max(12,int(np.ceil(len(a)*.75)))
    for ids in itertools.combinations(range(len(a)),3):
        sub=d[list(ids)]
        if abs(np.linalg.det(sub))<.1:continue
        coef=np.linalg.solve(sub,xy[list(ids)]);err=np.linalg.norm(xy-d@coef,axis=1);loss=float(np.sort(err)[:needed].mean())
        if best is None or loss<best[0]:best=loss,err
    if best is None:return None
    keep=best[1]<=3.
    if keep.sum()<needed:return None
    coef=np.linalg.lstsq(d[keep],xy[keep],rcond=-1)[0];err=np.linalg.norm(xy-d@coef,axis=1)
    if np.median(err[keep])>1.5:return None
    return np.c_[np.flatnonzero(keep),xy[keep]]


def estimate(frames565,masks,angles,cal):
    frames=np.asarray(frames565);sil=np.asarray(masks,bool);a=np.asarray(angles,float)
    if (frames.dtype!=np.uint16 or frames.shape!=sil.shape or frames.ndim!=3 or
            a.ndim!=1 or not np.isfinite(a).all() or len(frames)!=len(a) or
            min(frames.shape[1:])<16):
        raise ValueError('camera estimation requires synchronized saved photos, masks and angles')
    if len(a)<16:return None,dict(version=43,accepted=False,reason='insufficient_views')
    phases=np.sort(a%360);gaps=np.diff(np.r_[phases,phases[0]+360])
    if gaps.max()>45 or gaps.min()<1:return None,dict(version=43,accepted=False,reason='insufficient_turn_coverage')
    tracks,extract=photo_tracks(frames,sil);top=_extremum_track(sil,a)
    model,info=estimate_tracks(tracks,a,cal,frames.shape[1:],top)
    info['feature_extraction']=extract
    return model,info


def rotation(model,angle):
    e=float(model['inclination_rad']);r=float(model['roll_rad'])
    a=np.deg2rad(float(angle)*int(model['rotation_sign']));co,si=np.cos(a),np.sin(a)
    X=np.array([co,-si,0.]);Y=np.array([si,co,0.]);Z=np.array([0.,0.,1.])
    return np.stack([np.cos(r)*X-np.sin(r)*(np.sin(e)*Y-np.cos(e)*Z),
        np.sin(r)*X+np.cos(r)*(np.sin(e)*Y-np.cos(e)*Z),np.cos(e)*Y+np.sin(e)*Z])


def validate(model):
    if model.get('kind')!='photo_estimated_turntable_pinhole' or model.get('rotation_sign') not in (-1,1):
        raise ValueError('unsupported camera model or capture direction')
    if np.asarray(model['principal_point']).shape!=(2,):raise ValueError('invalid principal point')
    values=[model[k] for k in ('focal_px','inclination_rad','roll_rad','axis_col','v_ground','mm_per_px')]+list(model['principal_point'])
    if not np.isfinite(values).all() or model['focal_px']<=0 or model['mm_per_px']<=0:
        raise ValueError('invalid camera model')


def translation(model):
    cx,cy=model['principal_point'];f=model['focal_px'];scale=model['mm_per_px']
    return np.array([model['axis_col']-cx,model['v_ground']-cy,f])*scale


def project_world(points,model,angle):
    q=np.asarray(points,float)@rotation(model,angle).T+translation(model)
    z=q[...,2];safe=np.where(z>0,z,1.)
    uv=q[...,:2]/safe[...,None]*model['focal_px']+np.asarray(model['principal_point'])
    return uv[...,0],uv[...,1],z


def project_voxels(points,model,angle,n,pitch):
    p=np.asarray(points,float).copy();p[...,:2]-=(n-1)/2.;p*=pitch
    return project_world(p,model,angle)


def rays(u,v,model,angle,n,pitch):
    R=rotation(model,angle);cx,cy=model['principal_point'];f=model['focal_px']
    q=np.stack([(np.asarray(u)-cx)/f,(np.asarray(v)-cy)/f,np.ones_like(np.asarray(u),float)],axis=-1)
    direction=q@R;direction/=np.linalg.norm(direction,axis=-1,keepdims=True)
    origin=(-R.T@translation(model))/pitch+np.array([(n-1)/2.,(n-1)/2.,0.])
    return origin,direction


def carve(masks,angles,model,n,pitch):
    validate(model);src=np.asarray(masks,bool);a=np.asarray(angles,float)
    if (src.ndim!=3 or a.ndim!=1 or len(src)!=len(a) or not len(a) or
            not np.isfinite(a).all() or n<8 or not np.isfinite(pitch) or pitch<=0):
        raise ValueError('camera carving inputs mismatch')
    h,w=src.shape[1:];half=(n-1)/2.;x=(np.arange(n)[None,:]-half)*pitch;y=(np.arange(n)[:,None]-half)*pitch
    occ=np.ones((n,n,n),bool);t=translation(model);f=model['focal_px'];cx,cy=model['principal_point']
    for mask,angle in zip(src,a):
        R=rotation(model,angle);b=[t[k]+R[k,0]*x+R[k,1]*y for k in range(3)]
        for z in range(n):
            dep=b[2]+R[2,2]*z*pitch;safe=np.where(dep>0,dep,1.)
            u=np.floor(cx+f*(b[0]+R[0,2]*z*pitch)/safe+.5).astype(np.intp)
            v=np.floor(cy+f*(b[1]+R[1,2]*z*pitch)/safe+.5).astype(np.intp)
            good=(dep>0)&(u>=0)&(u<w)&(v>=0)&(v<h)
            occ[z]&=good&mask[np.clip(v,0,h-1),np.clip(u,0,w-1)]
    return occ


def _residual(p,tracks,angles,cal,side,shape,mode='all',records=False):
    invf,e,r,axis=p;invf/=1000.;axis+=float(cal['axis_col']);ground=float(cal['v_ground'])
    cx,cy=shape[1]/2.,shape[0]/2.;output=[];details=[]
    for track_index,obs in enumerate(tracks):
        a=np.deg2rad(angles[obs[:,0].astype(int)]*side);co=np.cos(a);si=np.sin(a);z=np.zeros(len(a));one=np.ones(len(a))
        X=np.stack([co,-si,z],1);Y=np.stack([si,co,z],1);Z=np.stack([z,z,one],1)
        U=np.cos(r)*X-np.sin(r)*(np.sin(e)*Y-np.cos(e)*Z)
        V=np.sin(r)*X+np.cos(r)*(np.sin(e)*Y-np.cos(e)*Z);D=(np.cos(e)*Y+np.sin(e)*Z)*invf
        u,v=obs[:,1],obs[:,2];A=np.stack([U-(u-cx)[:,None]*D,V-(v-cy)[:,None]*D],1).reshape(-1,3)
        b=np.stack([u-axis,v-ground],1).ravel();train=np.ones(len(obs),bool)
        if mode!='all':train[(track_index+len(obs)*int(mode[-1])//4)%len(obs)]=False
        ids=np.repeat(train,2);xyz=np.linalg.lstsq(A[ids],b[ids],rcond=-1)[0];den=1+D@xyz
        uv=np.stack([cx+(axis-cx+U@xyz)/den,cy+(ground-cy+V@xyz)/den],1)
        delta=uv-obs[:,1:];test=np.ones(len(obs),bool) if mode=='all' else train if mode.startswith('train') else ~train
        output.extend(delta[test].ravel());details.append(dict(xyz=xyz.tolist(),rms=float(np.sqrt(np.mean(delta**2)))))
    return (np.asarray(output),details) if records else np.asarray(output)


def _fit(initial,active,tracks,angles,cal,side,shape,mode='all'):
    p=np.asarray(initial,float);lam=.01
    def fun(q):return 3*np.tanh(_residual(q,tracks,angles,cal,side,shape,mode)/3)
    for _ in range(75):
        res=fun(p);jac=[]
        for k in active:
            step=[.001,.0001,.0001,.01][k];q=p.copy();q[k]+=step;jac.append((fun(q)-res)/step)
        J=np.stack(jac,1);lhs=J.T@J;step=np.linalg.solve(lhs+np.diag(np.maximum(np.diag(lhs),1))*lam,-J.T@res)
        q=p.copy();q[active]+=step;q[0]=np.clip(q[0],0,3.333333);q[1]=np.clip(q[1],-.5,.5);q[2]=np.clip(q[2],-.12,.12);q[3]=np.clip(q[3],-15,15)
        improvement=float((res**2).sum()-(fun(q)**2).sum())
        if improvement>0:
            p=q;lam=max(lam/3,1e-9)
            if improvement<1e-8:break
        else:lam=min(lam*5,1e12)
    return p,float(np.mean(fun(p)**2))


def estimate_tracks(tracks,angles,cal,shape=(480,800),top_track=None):
    """Select with genuinely unused observations, not volume or mask fit.

    Intrinsics assumptions remain explicit: square pixels, principal point at
    the image center, zero unmeasured lens distortion. Rejected alternatives
    are retained in diagnostics. No numerical success claims physical scale.
    """
    tracks=[np.asarray(t,float) for t in tracks if len(t)>=4]
    a=np.asarray(angles,float);info=dict(version=43,accepted=False,physical_accuracy_verified=False,
        principal_point_assumed_image_center=True,lens_distortion_measured=False,alternatives=[])
    if (a.ndim!=1 or not len(a) or not np.isfinite(a).all() or
            not np.isfinite([cal[k] for k in ('axis_col','v_ground','mm_per_px')]).all() or cal['mm_per_px']<=0):
        raise ValueError('invalid camera angles or scale')
    if len(tracks)<12:info['reason']='insufficient_independent_tracks';return None,info
    full=tracks+([] if top_track is None else [np.asarray(top_track,float)])
    if any(t.ndim!=2 or t.shape[1]!=3 or not np.isfinite(t).all() or
            (t[:,0]<0).any() or (t[:,0]>=len(a)).any() or
            (t[:,0]!=np.floor(t[:,0])).any() or len(np.unique(t[:,0]))!=len(t) for t in full):
        raise ValueError('invalid camera photo correspondences')
    for side in (1,-1):
        fits=[_fit([q,0,0,0],[0,1,2,3],full,a,cal,side,shape) for q in (.5,1.,2.)]
        p,loss=min(fits,key=lambda v:v[1]);record=dict(rotation_sign=side,parameters=p.tolist(),robust_training_loss=loss,folds=[])
        passed=.25<p[0]<3.25
        if top_track is not None:
            independent,_=_fit(p,[0,1,2,3],tracks,a,cal,side,shape)
            independent_agrees=(independent[0]>.05 and abs(independent[0]/p[0]-1)<.25 and
                abs(independent[1]-p[1])<np.deg2rad(3) and abs(independent[2]-p[2])<np.deg2rad(1) and abs(independent[3]-p[3])<2.)
            record['independent_non_extremum_parameters']=independent.tolist();record['independent_non_extremum_agrees']=bool(independent_agrees)
            passed &= independent_agrees
        fold_parameters=[]
        all_errors=[];all_flat=[]
        for fold in range(4):
            fitted,_=_fit(p,[0,1,2,3],full,a,cal,side,shape,'train'+str(fold));fold_parameters.append(fitted)
            err=np.linalg.norm(_residual(fitted,full,a,cal,side,shape,'test'+str(fold)).reshape(-1,2),axis=1)
            flat=np.linalg.norm(_residual([0,0,0,0],full,a,cal,side,shape,'test'+str(fold)).reshape(-1,2),axis=1)
            all_errors.append(err);all_flat.append(flat)
            improvement=float(np.median(flat)-np.median(err))
            ok=improvement>0
            record['folds'].append(dict(parameters=fitted.tolist(),median_test_error_px=float(np.median(err)),flat_median_test_error_px=float(np.median(flat)),
                rms_test_error_px=float(np.sqrt(np.mean(err**2))),test_observations=len(err),median_improves=bool(ok)))
        errors=np.asarray(all_errors);flat=np.asarray(all_flat)
        # Aggregate ALL four prescribed holdouts, then bootstrap whole tracks,
        # not correlated corners. One arbitrary holdout must not decide the fit.
        gains=(np.minimum(flat,8)-np.minimum(errors,8)).mean(0)
        rng=np.random.RandomState(4301);samples=rng.randint(0,len(gains),(1000,len(gains)))
        lower=float(np.percentile(gains[samples].mean(1),2.5));improvement=float(np.median(flat)-np.median(errors))
        stats=dict(median_test_error_px=float(np.median(errors)),flat_median_test_error_px=float(np.median(flat)),
            track_bootstrap_gain_lower_95_px=lower,track_win_fraction=float(np.mean(gains>0)),
            folds_with_positive_median_gain=sum(f['median_improves'] for f in record['folds']))
        record['all_prescribed_holdouts']=stats
        passed &= (np.median(errors)<=2.5 and improvement>=.4 and improvement/max(float(np.median(flat)),1e-9)>=.15 and
            lower>0 and np.mean(gains>0)>=.6 and stats['folds_with_positive_median_gain']>=3)
        fp=np.asarray(fold_parameters)
        stable=(fp[:,0].min()>.05 and fp[:,0].max()/fp[:,0].min()<1.2 and np.ptp(fp[:,1])<np.deg2rad(3) and np.ptp(fp[:,2])<np.deg2rad(1) and np.ptp(fp[:,3])<2.)
        record['folds_agree']=bool(stable);record['accepted']=bool(passed and stable);info['alternatives'].append(record)
    accepted=[d for d in info['alternatives'] if d['accepted']]
    if len(accepted)!=1:info['reason']='camera_or_capture_direction_not_identifiable';return None,info
    chosen=accepted[0];q,e,r,offset=chosen['parameters']
    model=dict(version=43,kind='photo_estimated_turntable_pinhole',focal_px=1000/q,inclination_rad=e,roll_rad=r,
        rotation_sign=chosen['rotation_sign'],axis_col=float(cal['axis_col'])+offset,v_ground=float(cal['v_ground']),
        mm_per_px=float(cal['mm_per_px']),principal_point=[shape[1]/2.,shape[0]/2.],physical_accuracy_verified=False,
        scale_source='existing_turntable_scale_not_independently_recalibrated',lens_distortion_measured=False)
    info.update(accepted=True,reason='unused_observations_and_independent_tracks_agree',selected_model=model,
        general_tracks=len(tracks),extremum_track_used=top_track is not None)
    return model,info


def colorize_faces(occ,pitch,frames565,masks,angles,model,channel_gains=None):
    """Intersect real camera rays with exposed voxel quads, including tops.

    Pixel ownership uses the nearest positive camera depth. Colors never cross
    a face's occlusion boundary, and missing observations remain explicitly gray.
    """
    from surface_photo import DIRECTIONS,exposed_faces,surface_directions
    validate(model);src=np.asarray(occ,bool);frames=np.asarray(frames565);sil=np.asarray(masks,bool)
    if src.ndim!=3 or len(set(src.shape))!=1 or not src.any() or frames.dtype!=np.uint16 or frames.shape!=sil.shape or len(frames)!=len(angles):
        raise ValueError('perspective face-color inputs mismatch')
    n=src.shape[0];half=(n-1)/2.;h,w=frames.shape[1:];groups=list(exposed_faces(src));surface=np.zeros_like(src)
    for z,y,x in groups:surface[z,y,x]=True
    z,y,x=np.nonzero(surface);flat=np.flatnonzero(surface);normals=surface_directions(src,(z,y,x));records=[]
    for (axis,sign),(z,y,x) in zip(DIRECTIONS,groups):
        component=2-axis;ids=np.searchsorted(flat,np.ravel_multi_index((z,y,x),src.shape));nn=normals[ids].copy()
        consistent=nn[:,component]*sign>1e-5;nn[~consistent]=0;nn[~consistent,component]=sign
        p=np.stack([x-half,y-half,z],1).astype(float);p[:,component]+=sign*.5;p*=pitch
        records.append(dict(point=p,component=component,sign=sign,normal=nn,surface_ids=ids,
            rgb=np.full((len(z),3),180,np.uint8),valid=np.zeros(len(z),bool),best=np.full(len(z),-np.inf,np.float32),
            view=np.full(len(z),-1,np.int16),previous_rgb=np.zeros((len(z),3),np.uint8),previous_seen=np.zeros(len(z),bool)))
    agreement=[];f=float(model['focal_px']);cx,cy=model['principal_point']
    for i,angle in enumerate(angles):
        R=rotation(model,angle);C=-R.T@translation(model);depth=np.full(h*w,np.inf,np.float32);samples=[]
        for rec in records:
            p=rec['point'];component=rec['component'];front=(C[component]-p[:,component])*rec['sign']>1e-8
            tangent=[j for j in range(3) if j!=component];corners=[]
            for da,db in ((-1,-1),(-1,1),(1,-1),(1,1)):
                q=p.copy();q[:,tangent[0]]+=da*pitch*.5;q[:,tangent[1]]+=db*pitch*.5
                u,v,z=project_world(q,model,angle);corners.append(np.stack([u,v],1));front &= z>0
            if not len(p) or not front.any():samples.append(None);continue
            corners=np.stack(corners,1);lower=np.ceil(corners.min(1)-1e-8).astype(np.intp);upper=np.floor(corners.max(1)+1e-8).astype(np.intp)
            uc,vc,_=project_world(p,model,angle);width=int(np.max(upper[:,0]-lower[:,0])+1);height=int(np.max(upper[:,1]-lower[:,1])+1)
            if max(width,height)>32:raise ValueError('projected voxel footprint too large for bounded face rasterizer')
            all_ids=[];all_pixels=[];all_depth=[]
            for dv in range(max(0,height)):
                vv=lower[:,1]+dv
                for du in range(max(0,width)):
                    uu=lower[:,0]+du;ok=front&(uu>=0)&(uu<w)&(vv>=0)&(vv<h)&(uu<=upper[:,0])&(vv<=upper[:,1]);ids=np.flatnonzero(ok)
                    ids=ids[sil[i,vv[ids],uu[ids]]]
                    if not len(ids):continue
                    direction=np.stack([(uu[ids]-cx)/f,(vv[ids]-cy)/f,np.ones(len(ids))],1)@R
                    den=direction[:,component];safe=np.where(np.abs(den)>1e-12,den,1.)
                    dep=(p[ids,component]-C[component])/safe;hit=C+dep[:,None]*direction
                    good=(np.abs(den)>1e-12)&(dep>0)&(np.abs(hit[:,tangent[0]]-p[ids,tangent[0]])<=pitch*.5+1e-7)&(np.abs(hit[:,tangent[1]]-p[ids,tangent[1]])<=pitch*.5+1e-7)
                    ids=ids[good];dep=dep[good];pixels=vv[ids]*w+uu[ids]
                    if not len(ids):continue
                    np.minimum.at(depth,pixels,dep.astype(np.float32));all_ids.append(ids);all_pixels.append(pixels);all_depth.append(dep)
            samples.append(None if not all_ids else (np.concatenate(all_ids),np.concatenate(all_pixels),np.concatenate(all_depth),uc,vc))
        pair_errors=[]
        for rec,sample in zip(records,samples):
            if sample is None:rec['previous_seen'].fill(False);continue
            ids,pixels,dep,uc,vc=sample;visible=dep<=depth[pixels]+max(1e-5,pitch*1e-4);ids=ids[visible];pixels=pixels[visible]
            if not len(ids):rec['previous_seen'].fill(False);continue
            distance=(pixels%w-uc[ids])**2+(pixels//w-vc[ids])**2;order=np.lexsort((distance,ids));ids=ids[order];pixels=pixels[order]
            _,first=np.unique(ids,return_index=True);ids=ids[first];pixels=pixels[first]
            camera=C-rec['point'][ids];camera/=np.linalg.norm(camera,axis=1,keepdims=True);score=np.sum(camera*rec['normal'][ids],axis=1)
            packed=frames[i].ravel()[pixels];r,g,b=(packed>>11)&31,(packed>>5)&63,packed&31
            rgb=np.stack(((r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)),1).astype(np.uint8);seen=rec['previous_seen'][ids]
            if seen.any():pair_errors.append(np.mean(np.abs(rgb[seen].astype(float)-rec['previous_rgb'][ids[seen]].astype(float)),axis=1))
            rec['previous_seen'].fill(False);rec['previous_seen'][ids]=True;rec['previous_rgb'][ids]=rgb
            eligible=(score>0)&(score>rec['best'][ids]+1e-7);ids=ids[eligible];rgb=rgb[eligible];score=score[eligible]
            rec['rgb'][ids]=rgb;rec['best'][ids]=score;rec['valid'][ids]=True;rec['view'][ids]=i
        errors=np.concatenate(pair_errors) if pair_errors else np.zeros(0)
        gap=0 if i==0 else abs((float(angle)-float(angles[i-1])+180)%360-180)
        if not 0<gap<=45:errors=np.zeros(0)
        agreement.append(dict(view=i,pairs=len(errors),median_error=None if not len(errors) else float(np.median(errors)),clipped_mean_error=None if not len(errors) else float(np.minimum(errors,64).mean())))
    point_rgb=np.full((len(flat),3),180,np.uint8);best=np.full(len(flat),-np.inf);counts=np.zeros(len(angles),np.int64)
    for rec in records:
        valid=rec['valid']
        if channel_gains is not None:rec['rgb'][valid]=np.clip(np.rint(rec['rgb'][valid].astype(float)*channel_gains),0,255).astype(np.uint8)
        ids=rec['surface_ids'];ok=valid&(rec['best']>best[ids]);point_rgb[ids[ok]]=rec['rgb'][ok];best[ids[ok]]=rec['best'][ok]
        counts+=np.bincount(rec['view'][valid],minlength=len(angles))
    diag=dict(implementation_version=43,camera_depth_sign=1,capture_rotation_sign=model['rotation_sign'],
        depth_direction_selection=dict(confirmed=True,selected_sign=1,method='right_handed_camera_from_crossvalidated_photo_correspondence'),
        geometry_modified=False,visibility='nearest_positive_ray_intersection_with_actual_exposed_quads',independent_face_colors=True,
        backfacing_photo_copies=0,adjacent_photo_agreement=agreement,photo_selected_faces_per_view=counts.tolist(),
        observed_faces=int(sum(d['valid'].sum() for d in records)),total_exposed_faces=int(sum(len(d['rgb']) for d in records)),
        unobserved_faces_stay_gray=True,physical_accuracy_verified=False)
    return dict(face_colors=[r['rgb'] for r in records],face_valid=[r['valid'] for r in records],face_views=[r['view'] for r in records],
        point_colors=point_rgb,point_valid=np.isfinite(best),diagnostics=diag)


def _estimate_worker(connection,frames_buffer,masks_buffer,shape,angles,cal):
    """Spawn owns all optional work; native OpenCV calls can be terminated."""
    try:
        frames=np.frombuffer(frames_buffer,dtype=np.uint16).reshape(shape)
        masks=np.frombuffer(masks_buffer,dtype=np.uint8).reshape(shape)
        frames.flags.writeable=False;masks.flags.writeable=False
        connection.send(('phase','提取照片特征与跨帧匹配'))
        tracks,extract=photo_tracks(frames,masks)
        connection.send(('phase','拟合透视参数并交叉验证'))
        top=_extremum_track(masks,angles)
        model,info=estimate_tracks(tracks,angles,cal,frames.shape[1:],top)
        info['feature_extraction']=extract
        connection.send(('result',(model,info)))
    except BaseException as exc:
        connection.send(('error',type(exc).__name__+': '+str(exc)))
    finally:connection.close()


def estimate_with_budget(frames565,masks,angles,cal,budget_s=20.,progress=None):
    """Optional estimate has a hard worker deadline, including native calls.

    Shared read-only input avoids serializing photographs through the pipe.
    Partial tracks or partially validated fits are NEVER accepted on timeout.
    """
    import multiprocessing as mp,time
    frames=np.asarray(frames565);sil=np.asarray(masks);a=np.asarray(angles,float)
    if (frames.dtype!=np.uint16 or frames.ndim!=3 or frames.shape!=sil.shape or
            a.ndim!=1 or len(a)!=len(frames) or not np.isfinite(a).all() or
            not np.isfinite(budget_s) or budget_s<=0):
        raise ValueError('预算或同圈相机输入无效')
    if len(a)<16:return None,dict(version=43,budget_version=57,accepted=False,reason='insufficient_views')
    phases=np.sort(a%360);gaps=np.diff(np.r_[phases,phases[0]+360])
    if gaps.max()>45 or gaps.min()<1:
        return None,dict(version=43,budget_version=57,accepted=False,reason='insufficient_turn_coverage')
    start=time.monotonic();deadline=start+float(budget_s);worker=None;receive=send=None
    info=dict(version=43,budget_version=57,accepted=False,budget_s=float(budget_s),
        reason='optional_camera_not_completed',baseline_retained=True,partial_fit_adopted=False)
    def stop_worker():
        if worker is None or worker.pid is None:return
        if worker.is_alive():worker.terminate()
        worker.join(timeout=.5)
        if worker.is_alive():
            if hasattr(worker,'kill'):worker.kill()
            else:
                import os,signal
                os.kill(worker.pid,signal.SIGKILL)
            worker.join(timeout=.5)
        if worker.is_alive():raise RuntimeError('相机估计工作进程未能退出')
    try:
        ctx=mp.get_context('spawn')
        source=ctx.RawArray('H',int(frames.size));region=ctx.RawArray('B',int(frames.size))
        np.frombuffer(source,dtype=np.uint16)[:]=frames.ravel()
        np.frombuffer(region,dtype=np.uint8)[:]=sil.astype(np.uint8,copy=False).ravel()
        if time.monotonic()>=deadline:
            info['reason']='camera_time_budget_exceeded_during_input_copy';return None,info
        receive,send=ctx.Pipe(duplex=False)
        worker=ctx.Process(target=_estimate_worker,args=(send,source,region,frames.shape,a,dict(cal)))
        worker.daemon=True;worker.start();send.close();send=None
        while time.monotonic()<deadline:
            if receive.poll(min(.1,max(0.,deadline-time.monotonic()))):
                try:kind,value=receive.recv()
                except EOFError:
                    info.update(reason='camera_worker_exited_without_result',worker_exitcode=worker.exitcode)
                    return None,info
                if kind=='phase':
                    info['last_worker_phase']=value
                    if progress is not None:progress.update('透视估计：'+value)
                elif kind=='result':
                    if time.monotonic()>=deadline:break
                    model,result=value
                    result.update(budget_version=57,budget_s=float(budget_s),elapsed_s=time.monotonic()-start,
                        baseline_retained=model is None,partial_fit_adopted=False)
                    return model,result
                elif kind=='error':
                    info.update(reason='camera_worker_failed',error=value);return None,info
            elif not worker.is_alive():
                if receive.poll():continue
                info.update(reason='camera_worker_exited_without_result',worker_exitcode=worker.exitcode)
                return None,info
        info['reason']='camera_time_budget_exceeded'
        return None,info
    except (OSError,RuntimeError,ImportError) as error:
        info.update(reason='camera_worker_unavailable',error=str(error));return None,info
    finally:
        stop_worker()
        if receive is not None:receive.close()
        if send is not None:send.close()
        info['elapsed_s']=time.monotonic()-start
