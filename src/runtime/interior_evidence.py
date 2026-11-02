"""Bounded interior evidence, not measured depth or uniform material filling.

Only enclosed negative components are audited. Binary PL evidence stays intact;
unknown rays are carried separately to a labelled conservative PS outer shell.
Python 3.6, NumPy only. No dense PS colour classification.
"""
import numpy as np

VERSION = 53


def measurement_window(calibration, ground):
    """Native camera coordinates; never translate a photograph or its axis."""
    bottom = int(np.floor(ground))
    if not 8 < bottom < 480:
        raise ValueError('台面行必须在画面内')
    if calibration is None:
        return (160, 0, 640, bottom)
    left, right = float(calibration['left_px']), float(calibration['right_px'])
    axis = float(calibration['axis_col'])
    if not np.isfinite([left, right, axis]).all() or not 0 <= left < axis < right < 800:
        raise ValueError('转台左右端点或转轴无效')
    if abs(float(calibration['v_ground'])-float(ground)) > .01:
        raise ValueError('测量框与台面标定不属于同一配置')
    # Small border allowance is a declared search domain, not an expanded mask.
    margin = max(8, int(np.ceil((right-left+1)*.03)))
    x0, x1 = max(4, int(np.floor(left))-margin), min(796, int(np.ceil(right))+margin+1)
    if not x0 < axis < x1:
        raise ValueError('转轴不在测量框内')
    return (x0, 0, x1, bottom)


def reconstruction_roi(window):
    x0, _, x1, _ = map(int, window)
    # Full image height is retained. Rectangles are padded to a square when
    # sampling PL masks; x/y share exactly the same pixel scale.
    width=int(np.ceil((x1-x0)/32.))*32
    x0=min(x0,800-width)
    return (x0, 0, x0+width, 480)


def _erode(a, radius):
    result=a.copy()
    for _ in range(radius):
        p=np.pad(result,1,mode='constant')
        result=np.ones_like(result)
        for dy in range(3):
            for dx in range(3):result &= p[dy:dy+a.shape[0],dx:dx+a.shape[1]]
    return result


def _centers(item, limit=8):
    a,b,c,d=item['box']
    region=np.zeros((d-b,c-a),bool)
    for y,l,r in item['runs']:region[y-b,l-a:r-a]=True
    inside=_erode(region,2)
    yy,xx=np.nonzero(inside)
    if not len(yy):return []
    take=np.linspace(0,len(yy)-1,min(limit,len(yy))).astype(int)
    return [(int(yy[k]+b),int(xx[k]+a)) for k in take]


def _patch_evidence(frame, reference, lut, noise, coords, driver):
    """Texture plus held-out pixels. Equal flat colour is NOT a background proof."""
    if not len(coords):return np.zeros(0,bool),np.zeros(0,bool),{}
    coords=np.asarray(coords,int);y,x=coords.T
    dy,dx=np.mgrid[-2:3,-2:3];dy,dx=dy.ravel(),dx.ravel()
    yy,xx=y[:,None]+dy,x[:,None]+dx
    raw=driver._rgb(frame[yy,xx]).astype(int)
    cur=np.stack([lut[c,raw[...,c]] for c in range(3)],-1).astype(float)
    native=np.array([8.,4.,8.])
    tolerance=np.broadcast_to(native[None,:]*1.25,(len(y),3)).copy()
    if noise is not None:tolerance+=3*np.asarray(noise[y,x],float)
    matched=np.zeros(len(y),bool);different=np.ones(len(y),bool)
    phase=(dy+dx)%2==0
    for sy,sx in ((0,0),(-1,0),(1,0),(0,-1),(0,1)):
        ref=driver._rgb(reference[yy+sy,xx+sx]).astype(float)
        ca=cur-cur.mean(1,keepdims=True);ra=ref-ref.mean(1,keepdims=True)
        cp=np.sum((ca/native)**2,axis=(1,2));rp=np.sum((ra/native)**2,axis=(1,2))
        corr=np.sum(ca*ra,axis=(1,2))/np.maximum(np.sqrt(np.sum(ca*ca,axis=(1,2))*np.sum(ra*ra,axis=(1,2))),1e-9)
        textured=(cp>50)&(rp>50)
        supported=textured&(corr>=.95)
        fold_different=np.ones(len(y),bool)
        for fold in (phase,~phase):
            # One constant offset learned on one fold must predict the other.
            offset=np.mean(cur[:,fold]-ref[:,fold],axis=1)
            error=cur[:,~fold]-ref[:,~fold]-offset[:,None,:]
            rms=np.sqrt(np.mean(error*error,axis=1))
            supported &= np.all(rms<=tolerance,axis=1)
            delta=np.mean(cur[:,fold]-ref[:,fold],axis=1)
            # Absolute colour discrepancy on both independent folds supports
            # an occluding surface, but not a measured surface depth.
            fold_different &= np.sum((delta/tolerance)**2,axis=1)>9.
        matched |= supported
        different &= fold_different
    different &= ~matched
    return matched,different,dict(patch_centers=len(y),positions=5,patch_size=5,
        flat_equal_colour_is_unknown=True,background_requires_texture_and_both_folds=True)


def audit(body,frame,reference,cfg,noise,window,driver,maximum_centers=256):
    """Keep strict mask immutable; annotate uncertainty before any 3D carving."""
    import subject_region
    x0,_,x1,bottom=map(int,window)
    local=body[:bottom,x0:x1].astype(bool)
    holes=[]
    for item in subject_region.components(~local,~local,driver):
        a,b,c,d=item['box']
        if a==0 or b==0 or c==local.shape[1] or d==bottom:continue
        holes.append(item)
    unknown=np.zeros_like(body,bool);bg=np.zeros_like(body,bool);fg=np.zeros_like(body,bool)
    coords=[];owners=[];budget=maximum_centers
    # Large components first. Unsampled holes remain unknown, never negative proof.
    holes.sort(key=lambda v:-v['area'])
    for i,item in enumerate(holes):
        points=_centers(item,min(8,budget)) if budget else []
        coords.extend((y,x+x0) for y,x in points);owners.extend([i]*len(points));budget-=len(points)
    matched,different,detail=_patch_evidence(frame,reference,cfg['lut'],noise,coords,driver)
    records=[];owners=np.asarray(owners,int)
    for i,item in enumerate(holes):
        hit=owners==i;n=int(hit.sum());nb=int(matched[hit].sum());nf=int(different[hit].sum())
        # Every sampled patch must agree; no majority from a texture boundary.
        kind='background' if n>=2 and nb==n else 'foreground_inferred' if n>=2 and nf==n else 'unknown'
        target=bg if kind=='background' else fg if kind=='foreground_inferred' else unknown
        for y,l,r in item['runs']:target[y,x0+l:x0+r]=True
        records.append(dict(area=int(item['area']),box_xyxy=[item['box'][0]+x0,item['box'][1],item['box'][2]+x0,item['box'][3]],
                            samples=n,background_texture_support=nb,occlusion_support=nf,state=kind))
    conservative=body|unknown|fg
    return dict(unknown=unknown,background=bg,inferred=fg,conservative=conservative,
        diagnostics=dict(version=VERSION,enclosed_regions=records,unknown_pixels=int(unknown.sum()),
            inferred_foreground_pixels=int(fg.sum()),confirmed_background_pixels=int(bg.sum()),
            strict_mask_changed=False,uniform_fill=False,physical_depth_recovered=False,
            conservative_shell_may_close_unresolved_true_holes=True,
            maximum_patch_centers=maximum_centers,**detail))


def _patches(frame,ys,xs,driver):
    """Decode RGB565 BEFORE subpixel interpolation."""
    ys=np.asarray(ys,float);xs=np.asarray(xs,float)
    dy,dx=np.mgrid[-2:3,-2:3]
    y=ys[:,None]+dy.ravel();x=xs[:,None]+dx.ravel()
    iy=np.floor(y).astype(int);ix=np.floor(x).astype(int)
    fy=(y-iy)[...,None];fx=(x-ix)[...,None]
    return (driver._rgb(frame[iy,ix])*(1-fy)*(1-fx)+driver._rgb(frame[iy+1,ix])*fy*(1-fx)+
            driver._rgb(frame[iy,ix+1])*(1-fy)*fx+driver._rgb(frame[iy+1,ix+1])*fy*fx)


def _scores(source,targets):
    a=source-source.mean(0,keepdims=True);b=targets-targets.mean(1,keepdims=True)
    return np.sum(a[None]*b,axis=(1,2))/np.maximum(np.sqrt(np.sum(a*a)*np.sum(b*b,axis=(1,2))),1e-9)


def _match(source,frame,y,x,driver,mask=None,small=False):
    # Fixed search budget; no all-image feature extraction or guessed same-pixel match.
    dy,dx=np.mgrid[-2:3,-32:33] if not small else np.mgrid[-2:3,-3:4]
    yy=y+dy.ravel();xx=x+dx.ravel();legal=(yy>=4)&(yy<475)&(xx>=4)&(xx<795)
    if mask is not None:
        legal &= mask[np.clip(np.rint(yy).astype(int),0,479),np.clip(np.rint(xx).astype(int),0,799)]
    yy,xx=yy[legal],xx[legal]
    if not len(yy):return None
    scores=_scores(source,_patches(frame,yy,xx,driver));best=int(np.argmax(scores))
    distant=(yy-yy[best])**2+(xx-xx[best])**2>9
    second=float(scores[distant].max()) if distant.any() else -1.
    if scores[best]<.95 or scores[best]-second<.08:return None
    y,x=float(yy[best]),float(xx[best])
    # Continuous local optimum, followed by an independent RGB-patch recheck.
    for axis in (0,1):
        ys=np.full(3,y);xs=np.full(3,x)
        (ys if axis==0 else xs)[:]+=np.array([-.5,0.,.5])
        s=_scores(source,_patches(frame,ys,xs,driver));den=s[0]-2*s[1]+s[2]
        offset=float(np.clip(.25*(s[0]-s[2])/den,-.5,.5)) if den< -1e-6 else 0.
        if axis==0:y+=offset
        else:x+=offset
    if _scores(source,_patches(frame,[y],[x],driver))[0]<.95:return None
    return y,x


def audit_multiview(frames,result,angles,axis,driver,maximum_points=24):
    """Sparse three-view correspondences; never turn a whole ray into material.

    Parallel-camera consistency is conditional, not a replacement calibration.
    Accepted observations are saved separately and do not change either mask.
    """
    started=__import__('time').monotonic();records=[];tested=0
    if axis is None or len(frames)<3:
        return dict(performed=False,reason='axis_or_three_views_unavailable',accepted=[],tested_points=0)
    angles=np.asarray(angles,float)
    for i,frame in enumerate(frames):
        if tested>=maximum_points:break
        points=np.argwhere(_erode(result['interior_unknown_masks'][i].astype(bool),2))
        if not len(points):continue
        # At most64 sparse texture probes; one distinctive point per view.
        points=points[np.linspace(0,len(points)-1,min(64,len(points))).astype(int)]
        patches=_patches(frame,points[:,0],points[:,1],driver)
        power=np.mean((patches-patches.mean(1,keepdims=True))**2,axis=(1,2))
        k=int(np.argmax(power))
        if power[k]<25:continue
        y,x=map(float,points[k]);source=patches[k];tested+=1
        delta=(angles-angles[i]+180)%360-180
        before=np.flatnonzero((delta<=-5)&(delta>=-30));after=np.flatnonzero((delta>=5)&(delta<=30))
        if not len(before) or not len(after):continue
        neighbors=[int(before[np.argmin(abs(delta[before]))]),int(after[np.argmin(abs(delta[after]))])]
        observations=[(i,y,x)];good=True
        for j in neighbors:
            match=_match(source,frames[j],y,x,driver,result['photo_strong_masks'][j].astype(bool))
            if match is None:good=False;break
            yy,xx=match;target=_patches(frames[j],[yy],[xx],driver)[0]
            reverse=_match(target,frame,y,x,driver,small=True)
            if reverse is None or np.hypot(reverse[0]-y,reverse[1]-x)>.75 or np.hypot(yy-y,xx-x)<1.5:
                good=False;break
            observations.append((j,yy,xx))
        if not good:continue
        ids=np.asarray([v[0] for v in observations]);us=np.array([v[2] for v in observations]);vs=np.array([v[1] for v in observations])
        theta=np.deg2rad(angles[ids]);design=np.stack([np.cos(theta),-np.sin(theta)],axis=1)
        xy=np.linalg.lstsq(design,us-float(axis),rcond=-1)[0]
        error=float(np.max(np.abs(design@xy+float(axis)-us)))
        if error>.75 or np.ptp(vs)>1.5:continue
        records.append(dict(view=int(i),source_uv=[x,y],observations_iyx=[list(v) for v in observations],
            projection_error_px=error,parallel_xy_in_camera_pixels=xy.tolist(),
            pixel_foreground_support_only=True,physical_depth_verified=False))
    return dict(performed=True,tested_points=tested,maximum_points=maximum_points,accepted=records,
        source_masks_modified=False,whole_rays_filled=False,projection_hypothesis='parallel_camera_not_physical_calibration',
        elapsed_host_or_board_s=__import__('time').monotonic()-started)
