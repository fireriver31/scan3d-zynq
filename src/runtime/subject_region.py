"""Fixed measurement window. PL features; bounded PS binary region selection.

Outside the window is an acquisition nuisance reference, never an object.
There is no fill, contour expansion, source-image warp or object-class prior.
"""
import time
import hashlib
import json
from pathlib import Path
import numpy as np
import importlib
import subject_evidence
if Path(subject_evidence.__file__).resolve().parent != Path(__file__).resolve().parent:
    raise RuntimeError('subject_evidence.py必须与subject_region.py同目录')
subject_evidence=importlib.reload(subject_evidence)
if getattr(subject_evidence,'VERSION',0)<76:
    raise RuntimeError('请同时覆盖v76的subject_evidence.py')

VERSION = 76


class NoStrongSeed(ValueError):
    """Initial evidence cannot seed ownership; not an empty-object verdict."""
    pass
ROI = (160, 0, 640, 480)


def background_trend(frame,reference,parameters,ground,driver):
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    y,x=np.mgrid[4:int(ground):8,4:796:8]
    cur=driver._rgb(frame[y,x]);ref=driver._rgb(reference[y,x])
    current=np.stack([parameters['lut'][c,cur[...,c]] for c in range(3)],-1)
    exterior=(x[0]<left)|(x[0]>=right)
    if not exterior.any():
        return dict(requires_review=False,reason='no_exterior_columns_at_trend_stride_rim_validation_used')
    residual=(current-ref)[:,exterior]
    column=np.median(residual,axis=0)
    within=np.median(np.abs(residual-column[None,:,:]),axis=(0,1))
    between=.5*(np.percentile(column,90,axis=0)-np.percentile(column,10,axis=0))
    return dict(requires_review=bool(np.any(between>3*np.maximum(within,np.array([8.,4.,8.])/np.sqrt(12.)))),
                between_column_MAD=between.tolist(),within_column_MAD=within.tolist())


def sample(reference, y, x, driver):
    iy,ix = np.floor(y).astype(int),np.floor(x).astype(int)
    fy,fx = (y-iy)[...,None],(x-ix)[...,None]
    rgb = driver._rgb
    return (rgb(reference[np.clip(iy,0,479),np.clip(ix,0,799)])*(1-fy)*(1-fx)+
            rgb(reference[np.clip(iy+1,0,479),np.clip(ix,0,799)])*fy*(1-fx)+
            rgb(reference[np.clip(iy,0,479),np.clip(ix+1,0,799)])*(1-fy)*fx+
            rgb(reference[np.clip(iy+1,0,479),np.clip(ix+1,0,799)])*fy*fx)


def register_background(frame,reference,cfg,ground,driver):
    """A bounded background-only gradient observation, with held-out sides."""
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    yy,xx=np.mgrid[3:int(ground)-3:4,4:796:16]
    hit=(xx<left-4)|(xx>=right+4);y,x=yy[hit],xx[hit]
    def observed(dy,dx):
        raw=driver._rgb(frame[y+dy,x+dx])
        return np.stack([cfg['lut'][c,raw[:,c]] for c in range(3)],1).astype(float)
    gy=observed(1,0)-observed(-1,0)
    gx=observed(0,1)-observed(0,-1)
    ry=sample(reference,y+1,x,driver)-sample(reference,y-1,x,driver)
    rx=sample(reference,y,x+1,driver)-sample(reference,y,x-1,driver)
    edge=np.sum(ry*ry+rx*rx,axis=1)>64
    train=((y//24+x//24)%2)==0
    info=dict(accepted=False,position_yx=[0.,0.],samples=int(edge.sum()),
              source_image_modified=False,physical_camera_calibration_corrected=False)
    if min(int((edge&train).sum()),int((edge&~train).sum()))<32:
        info['reason']='insufficient_static_background_texture';return reference,info
    def losses(pos):
        dy,dx=pos
        a=sample(reference,y+dy+1,x+dx,driver)-sample(reference,y+dy-1,x+dx,driver)
        b=sample(reference,y+dy,x+dx+1,driver)-sample(reference,y+dy,x+dx-1,driver)
        return np.minimum(np.sum(((gy-a)**2+(gx-b)**2)/np.array([16.,4.,16.]),axis=1),40.)
    zero=losses((0.,0.));position=[0.,0.]
    for _ in range(1):
        for axis in (0,1):
            candidates=[]
            for delta in np.linspace(-1.,1.,9):
                pos=list(position);pos[axis]=float(delta)
                loss=losses(pos)
                candidates.append((float(loss[edge&train].mean()),sum(v*v for v in pos),pos))
            position=min(candidates,key=lambda v:(v[0],v[1]))[2]
    after=losses(position);accepted=position!=[0.,0.];heldout=[]
    for side in (x<left,x>=right):
        test=edge&~train&side
        blocks=(y//24)*34+x//24
        gains=np.asarray([float((zero-after)[test&(blocks==k)].mean()) for k in np.unique(blocks[test])])
        sem=float(gains.std()/np.sqrt(max(len(gains),1))) if len(gains) else 0.
        ok=bool(len(gains)>=4 and gains.mean()>max(.02,3*sem))
        accepted &= ok
        heldout.append(dict(blocks=len(gains),improvement=float(gains.mean()) if len(gains) else 0.,standard_error=sem,accepted=ok))
    info.update(accepted=bool(accepted),position_yx=position if accepted else [0.,0.],heldout_sides=heldout)
    if not accepted:return reference,info
    y,x=np.indices(reference.shape,dtype=float)
    rgb=np.clip(sample(reference,y+position[0],x+position[1],driver),0,255)
    q=np.rint(rgb*np.array([31.,63.,31.])/255.).astype(np.uint16)
    return (q[...,0]<<11)|(q[...,1]<<5)|q[...,2],info


def exterior_parameter_mask(frame,reference,ground,driver):
    """A large exterior change must not be learned as the sensor noise floor.

    Only sparse RGB samples are examined. Ambiguous offset modes prefer the
    camera's existing response, and ordinary small residuals retain the old fit.
    """
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    y,x=np.mgrid[:480:8,:800:4]
    anchor=(y<int(ground))&((x<left)|(x>=right))
    current=driver._rgb(frame[::8,::4]);ref=driver._rgb(reference[::8,::4])
    residual=current-ref;values=residual[anchor]
    step=np.array([16.,8.,16.])
    bins=np.rint(values/step).astype(int)
    unique,inverse,counts=np.unique(bins,axis=0,return_inverse=True,return_counts=True)
    # A stable cluster is usually near zero under the locked-camera workflow.
    candidates=np.flatnonzero(counts>=counts.max()*.9)
    chosen=min(candidates,key=lambda k:(float(np.sum(unique[k].astype(float)**2)),-int(counts[k])))
    center=np.median(values[inverse==chosen],axis=0)
    valid=np.all(np.abs(residual-center)<=step*1.5,axis=2)
    kept=int((valid&anchor).sum());all_count=int(anchor.sum())
    severe=np.sum(((values-center)/np.array([8.,4.,8.]))**2,axis=1)>16.**2
    applied=bool(kept>=256 and kept<all_count*.9 and severe.mean()>.1)
    info=dict(applied=applied,sparse_exterior_samples=all_count,retained_samples=kept if applied else all_count,
              coarse_offset_cluster_RGB=center.tolist(),large_exterior_change_fraction=float(severe.mean()),
              filtering_is_parameter_estimation_only=True)
    return valid if applied else None,info


def validate_exterior_parameters(frame,reference,ground,noise,cfg,driver):
    """Validate exterior estimates on the declared empty measurement rim.

    The user keeps the complete rotating object inside the window. Sparse
    points just inside its rim can therefore veto an unrelated exterior change
    being applied as a global camera response or an excessive sensor noise.
    """
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    rows=np.arange(4,max(5,int(ground)-4),8)
    columns=np.arange(left+4,right-4,8)
    y=np.r_[rows,rows,np.full(len(columns),4)]
    x=np.r_[np.full(len(rows),left+4),np.full(len(rows),right-5),columns]
    raw=driver._rgb(frame[y,x]);ref=driver._rgb(reference[y,x])
    corrected=np.stack([cfg['lut'][c,raw[:,c]] for c in range(3)],axis=-1)
    native=np.array([8.,4.,8.])
    before=float(np.median(np.mean(np.abs(raw-ref)/native,axis=1)))
    raw_p95=float(np.percentile(np.mean(np.abs(raw-ref)/native,axis=1),95))
    after=float(np.median(np.mean(np.abs(corrected-ref)/native,axis=1)))
    estimated=np.stack([65536./cfg['inverse_variance'][c,ref[:,c]>>4] for c in range(3)],axis=-1)
    observed=np.percentile(np.abs(corrected-ref),68.27,axis=0)
    sigma=np.maximum(native/np.sqrt(12.),observed)
    if noise is not None:sigma=np.maximum(sigma,np.percentile(noise[y,x],90,axis=0))
    inflated=float(np.mean((estimated>(sigma[None,:]*16.)**2).sum(axis=1)>=2))
    disagreement=bool(after>before+1. or inflated>.5)
    inside_changed=bool(raw_p95>1.)
    rejected=bool(disagreement and not inside_changed)
    info=dict(samples=len(y),rim_is_background_under_user_scope=True,
              raw_median_native_code_loss=before,corrected_median_native_code_loss=after,
              raw_p95_native_code_loss=raw_p95,interior_background_changed=inside_changed,
              excessive_noise_fraction=inflated,exterior_estimate_rejected=rejected,
              requires_spatial_review=bool(disagreement and inside_changed))
    if rejected:
        # The current exterior cannot identify the camera response. Reuse the
        # measured empty-scene temporal noise; the actual PL still gets FRAME.
        cfg=driver.prepare_parameters(reference,reference,ground,noise)
        cfg['info']['parameter_source']='empty_scene_temporal_noise_after_exterior_rim_disagreement'
    return cfg,info


def reference_family_explains_rim(frame,reference,cfg,ground,noise,driver):
    """Select the cheap reference family using only declared background samples.

    This is a sparse nuisance validation, not a PS foreground classifier.
    A light change that cannot be explained within native-code/noise intervals
    still enters the single validated appearance fit.
    """
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    rows=np.arange(3,max(4,int(ground)-3),4);cols=np.arange(left+4,right-4,8)
    y=np.r_[rows,rows,np.full(len(cols),3)]
    x=np.r_[np.full(len(rows),left+4),np.full(len(rows),right-5),cols]
    side=np.r_[np.zeros(len(rows),int),np.ones(len(rows),int),np.full(len(cols),2,int)]
    raw=driver._rgb(frame[y,x]);current=np.stack([cfg['lut'][c,raw[:,c]] for c in range(3)],axis=-1)
    tolerance=np.broadcast_to(np.array([9.,5.,9.]),current.shape).copy()
    if noise is not None:tolerance+=2*noise[y,x]
    compatible=np.zeros(len(y),bool)
    for dy,dx in ((0,0),(-1,0),(1,0),(0,-1),(0,1)):
        expected=driver._rgb(reference[y+dy,x+dx])
        compatible |= np.all(np.abs(current-expected)<=tolerance,axis=1)
    fractions=[float(compatible[side==k].mean()) for k in (0,1,2)]
    return dict(accepted=bool(min(fractions)>=.95 and compatible.mean()>=.98),
                samples=len(y),side_compatible_fractions=fractions,
                total_compatible_fraction=float(compatible.mean()),
                region='inside_fixed_window_rim_assumed_background',
                positions_yx=[[0,0],[-1,0],[1,0],[0,-1],[0,1]],
                source_pixels_modified=False)


def components(mask, strong, driver):
    runs, labels, areas, strength = driver._regions(mask, strong)
    groups = {}
    for y, a, b, node in runs:
        r = int(labels[node])
        if r not in groups:
            groups[r] = dict(area=int(areas[r]), strong=int(strength[r]),
                             box=[mask.shape[1], mask.shape[0], 0, 0], runs=[])
        item = groups[r]
        box = item['box']
        item['box'] = [min(box[0], a), min(box[1], y), max(box[2], b), max(box[3], y+1)]
        item['runs'].append((y, a, b))
    return list(groups.values())


def subject_context(strong, driver,axis_col=None):
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    core = strong.copy()
    for offset in (1,2):
        core[offset:] &= strong[:-offset]; core[:-offset] &= strong[offset:]
        core[:,offset:] &= strong[:,:-offset]; core[:,:-offset] &= strong[:,offset:]
    core[:2]=False; core[-2:]=False; core[:,:2]=False;core[:,-2:]=False
    groups = components(core,core,driver)
    if not groups:
        groups = components(strong,strong,driver)
    if not groups:
        return None
    h,w = strong.shape
    axis=w*.5 if axis_col is None else float(axis_col)-float(left)
    def score(v):
        a,b,c,d = v['box']
        return v['area']/(1.+4.*((.5*(a+c)-axis)/w)**2)
    primary = max(groups,key=score)
    box = list(primary['box'])
    a,b,c,d = box
    reach = max(16.,.35*max(c-a,d-b))
    for item in groups:
        x0,y0,x1,y1 = item['box']
        dx,dy = max(a-x1,x0-c,0),max(b-y1,y0-d,0)
        if item['area']>=max(6,primary['area']*.01) and dx*dx+dy*dy<=reach*reach:
            box=[min(box[0],x0),min(box[1],y0),max(box[2],x1),max(box[3],y1)]
    a,b,c,d = box
    mx,my = 32+.1*(c-a),32+.1*(d-b)
    return [max(0,int(a-mx)),max(0,int(b-my)),min(w,int(np.ceil(c+mx))),min(h,int(np.ceil(d+my)))]


def select(mask, strong, driver, location_strong=None,axis_col=None):
    """A main body plus nearby separated parts; never fill a silhouette hole."""
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    groups = [v for v in components(mask, strong, driver) if v['strong'] >= 6]
    if not groups:
        raise ValueError('测量框内没有足够的物体前景证据；查看已保存的彩照与掩膜')
    h, w = mask.shape
    axis=w*.5 if axis_col is None else float(axis_col)-float(left)
    def score(v):
        a,b,c,d = v['box']
        center = .5*(a+c)
        return v['strong']/(1.+4.*((center-axis)/w)**2)
    primary = max(groups, key=score)
    # Erosion is used ONLY to locate a sturdy body, never as the output mask.
    # A one-pixel table-edge bridge must not expand the object's search bounds.
    core = strong.copy() if location_strong is None else location_strong.copy()
    core[1:] &= strong[:-1]; core[:-1] &= strong[1:]
    core[:,1:] &= strong[:,:-1]; core[:,:-1] &= strong[:,1:]
    cores = [v for v in components(core,core,driver) if v['area']>=6]
    anchor = max(cores,key=score) if cores else primary
    a,b,c,d = anchor['box']
    margin = max(12., .20*max(c-a,d-b))
    kept, ignored = [], []
    body = np.zeros_like(mask, dtype=bool)
    for item in groups:
        x0,y0,x1,y1 = item['box']
        dx = max(a-x1,x0-c,0)
        dy = max(b-y1,y0-d,0)
        take = dx*dx+dy*dy <= margin*margin
        (kept if take else ignored).append({k:v for k,v in item.items() if k!='runs'})
        if take:
            for y,x0,x1 in item['runs']:
                body[y,x0:x1] = True
    return body, dict(main_box_local_xyxy=anchor['box'], retained_components=kept,
                      ignored_remote_components=ignored, main_component_margin_px=margin,
                      ignored_remote_pixels=int(sum(v['area'] for v in ignored)))


def fast_binary(strong, possible, ground, driver,axis_col=None):
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    bottom = min(480,int(np.floor(ground)))
    s,p = strong[:bottom,left:right],possible[:bottom,left:right]
    core=s.copy()
    for _ in range(3):
        padded=np.pad(core,1,mode='constant');core=np.ones_like(core)
        for dy in range(3):
            for dx in range(3):core &= padded[dy:dy+len(s),dx:dx+s.shape[1]]
    groups=[v for v in components(core,core,driver) if v['area']>=6]
    if not groups:
        groups=[v for v in components(s,s,driver) if v['area']>=6]
    if not groups:raise NoStrongSeed('测量框内没有足够的主体证据')
    h,w=p.shape
    axis=w*.5 if axis_col is None else float(axis_col)-float(left)
    def score(v):
        a,b,c,d=v['box']
        return v['area']/(1.+4.*((.5*(a+c)-axis)/w)**2)
    primary=max(groups,key=score);a,b,c,d=primary['box']
    margin=max(12.,.2*max(c-a,d-b))
    fg=np.zeros_like(p);bg=np.zeros_like(p)
    for item in groups:
        x0,y0,x1,y1=item['box'];dx,dy=max(a-x1,x0-c,0),max(b-y1,y0-d,0)
        target=fg if dx*dx+dy*dy<=margin*margin else bg
        for y,x0,x1 in item['runs']:target[y,x0:x1]=True
    # This is the USER's declared measurement domain, not evidence of cropping.
    # Its exterior cannot own an object; remote changes are nuisance background.
    bg[0]=p[0];bg[:,0]=p[:,0];bg[:,-1]=p[:,-1]
    fg &= ~bg
    body=np.zeros_like(p);mixed=np.zeros_like(p);ignored=0
    for item in components(p,s,driver):
        has_fg=any(fg[y,x0:x1].any() for y,x0,x1 in item['runs'])
        has_bg=any(bg[y,x0:x1].any() for y,x0,x1 in item['runs'])
        target=mixed if has_fg and has_bg else body if has_fg else None
        if target is None:ignored+=item['area'];continue
        for y,x0,x1 in item['runs']:target[y,x0:x1]=True
    unresolved=0;iterations=0
    if mixed.any():
        limit=np.iinfo(np.uint16).max
        df=np.full(p.shape,limit,np.uint16);db=df.copy()
        ff=fg&mixed;fb=bg&mixed;df[ff]=0;db[fb]=0
        def neighbors(a):
            b=np.zeros_like(a)
            b[1:] |= a[:-1];b[:-1] |= a[1:];b[:,1:] |= a[:,:-1];b[:,:-1] |= a[:,1:]
            return b
        for step in range(1,65):
            ff=neighbors(ff)&mixed&(df==limit)
            fb=neighbors(fb)&mixed&(db==limit)
            df[ff]=step;db[fb]=step;iterations=step
            if not ff.any() and not fb.any():break
        body |= mixed&(df<=db)&(df!=limit)
        unreached=mixed&(df==limit)&(db==limit)
        # A long thin branch with only foreground ownership remains intact.
        for item in components(unreached,s&unreached,driver):
            f=b=False
            for y,x0,x1 in item['runs']:
                if y:
                    f |= bool((df[y-1,x0:x1]<limit).any());b |= bool((db[y-1,x0:x1]<limit).any())
                if y+1<len(p):
                    f |= bool((df[y+1,x0:x1]<limit).any());b |= bool((db[y+1,x0:x1]<limit).any())
                if x0:
                    f |= bool(df[y,x0-1]<limit);b |= bool(db[y,x0-1]<limit)
                if x1<p.shape[1]:
                    f |= bool(df[y,x1]<limit);b |= bool(db[y,x1]<limit)
            if f and not b:
                for y,x0,x1 in item['runs']:body[y,x0:x1]=True
            elif f and b:unresolved+=item['area']
    border = bool(body[0].any() or body[:,0].any() or body[:,-1].any())
    support = int((body & s).sum())
    ratio = float(body.sum())/max(support,1)
    info=dict(candidate_touches_window=border, possible_to_strong_ratio=ratio,
              main_box_local_xyxy=primary['box'],ignored_remote_pixels=int(ignored),
              binary_competition_iterations=iterations,unresolved_candidate_pixels=unresolved,
              needs_partition=bool(unresolved or ratio>1.5),
              ownership='fixed_window_main_core_vs_remote_and_exterior',
              _main_core=fg,
              hole_filling=False,outline_expansion=False)
    full = np.zeros((480,800),bool)
    full[:bottom,left:right] = body
    return full, info


def partition(device, frame, reference, cfg, ground, noise, driver, progress,
              strong, possible, packed, status,evidence_sink=None,axis_col=None,
              force_confidence=False):
    """One fixed-ROI graph, <=256 patch centers, 3 PL passes in total."""
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    import edge_partition as edge
    import background_witness as witness
    started = time.monotonic()
    bottom = int(np.floor(ground))
    identity = np.tile(np.arange(256,dtype=np.uint8),(3,1))
    gradients = []
    records = [dict(reference=reference, parameters=cfg, packed=packed, status=status)]
    for axis in (1,0):
        other = np.empty_like(frame)
        if axis == 1:
            other[:,:-1] = frame[:,1:]; other[:,-1] = frame[:,-1]
        else:
            other[:-1] = frame[1:]; other[-1] = frame[-1]
        parameters = dict(lut=identity,
            inverse_variance=np.tile(np.asarray([1024,4096,1024],np.uint16)[:,None],(1,16)),
            low=int(np.ceil((81./64*2+25./16)*65536)), high=int(np.ceil(12.838*65536)))
        parameters['_within_reference_group']=axis==0
        if progress:
            progress.update('PL测量框内轮廓证据', '边缘 %d/2' % len(records))
        record=dict(reference=other,parameters=parameters,
                    kind='source_horizontal_gradient' if axis==1 else 'source_vertical_gradient')
        if evidence_sink is not None:
            evidence_sink['records'].append(record)
            evidence_sink.update(parameters=parameters,modeled_reference=other)
        s,p,st,wp = device.classify(frame,other,parameters)
        record.update(packed=wp,status=st)
        gradients.append((s[:bottom,left:right],p[:bottom,left:right]))
        records.append(record)
    s = strong[:bottom,left:right]
    p = possible[:bottom,left:right]
    state = s.astype(np.uint8)*2+p.astype(np.uint8)
    if progress:
        progress.update('测量框内二值区域划分')
    labels = edge.regions(state if force_confidence else np.zeros_like(state),gradients[0][1],gradients[1][1])
    if not force_confidence and int(labels.max())+1>1024:
        labels = edge.regions(state,gradients[0][1],gradients[1][1])
    n = int(labels.max())+1
    if n>16384:
        raise ValueError('测量框内图像过于破碎，超过区域预算；诊断已保留')
    flat = labels.ravel()
    area = np.bincount(flat,minlength=n)
    positive = np.bincount(flat,weights=s.ravel(),minlength=n)
    weak = np.bincount(flat,weights=(p&~s).ravel(),minlength=n)
    negative = area-positive-weak
    # Large regions dominate both shape and work. Tiny remote changes are
    # disposed by main-body selection, not individually texture-validated.
    context_box=subject_context(s,driver,axis_col)
    context=np.zeros_like(s)
    if context_box is None:context[:]=True
    else:
        a,b,c,d=context_box;context[b:d,a:c]=True
    context_nodes=np.bincount(flat,weights=context.ravel(),minlength=n)>0
    eligible = np.flatnonzero(((positive+weak)>0)&context_nodes)
    chosen = eligible[np.argsort(-area[eligible],kind='mergesort')[:64]]
    order = np.argsort(flat,kind='mergesort')
    ends = np.cumsum(area); starts = ends-area
    coords, owners = [], []
    for r in chosen:
        indices = order[starts[r]+np.linspace(0,area[r]-1,min(4,area[r])).astype(int)]
        coords.extend((int(k//width),int(k%width+left)) for k in indices)
        owners.extend([int(r)]*len(indices))
    bg = np.zeros(n,bool)
    if coords:
        if progress:
            progress.update('主体附近有限背景核验', '%d个采样点' % len(coords))
        compatible = witness.background_compatible(frame,reference,cfg['lut'],np.asarray(coords),noise,
                                                    radius=4,fractional=True,progress=progress)
        tested = np.bincount(owners,minlength=n)
        supported = np.bincount(owners,weights=compatible,minlength=n)
        bg = (tested>0)&(supported>=np.minimum(tested,2))&(supported>=tested*.5)
    fg_cost = np.where(bg,area.astype(float)*8.,-np.log(.05)*negative)
    bg_cost = np.where(bg,0.,-np.log(.005)*positive-np.log(.05)*weak)
    flow = edge.Flow(n+2)
    for i in range(n):
        flow.add(n,i,bg_cost[i]); flow.add(i,n+1,fg_cost[i])
    pairs, weights = [], []
    quiet = -np.log(.005)/2
    for axis,(gs,gp) in enumerate(gradients):
        if axis==0:
            a,b = labels[:,:-1],labels[:,1:]; gs,gp = gs[:,:-1],gp[:,:-1]
        else:
            a,b = labels[:-1],labels[1:]; gs,gp = gs[:-1],gp[:-1]
        hit = a!=b
        lo,hi = np.minimum(a[hit],b[hit]),np.maximum(a[hit],b[hit])
        pairs.append(lo*n+hi)
        weights.append(np.where(gs[hit],quiet*np.exp(-12.838/2),
                                np.where(gp[hit],quiet*np.exp(-(81./64*2+25./16)/2),quiet)))
    unique,index = np.unique(np.concatenate(pairs),return_inverse=True)
    energy = np.bincount(index,weights=np.concatenate(weights))
    for k,w in zip(unique,energy):
        a,b = int(k//n),int(k%n)
        flow.add(a,b,w); flow.add(b,a,w)
    if progress:
        progress.update('主体区域二值优化')
    reachable = flow.solve(n,n+1)
    foreground = np.array([i in reachable for i in range(n)])
    result,selection = select(foreground[labels],s,driver,axis_col=axis_col)
    full = np.zeros((480,800),bool); full[:bottom,left:right] = result
    info = dict(regions=n,patch_centers=len(coords),maximum_patch_centers=256,
                texture_sampling_box_local_xyxy=context_box,
                foreground_pixels=int(full.sum()),selection=selection,
                elapsed_host_or_board_s=time.monotonic()-started,
                processing_route='fixed_ROI_bounded_region_refinement',
                boundary_is_user_scope_not_full_object_certification=True)
    return full,info,records


def _hash(a):
    return hashlib.sha256(np.asarray(a).astype('<u2',copy=False).tobytes()).hexdigest()


def discard_unattached_speckles(body,driver):
    """Keep complete components containing a core; never erode output pixels.

    Connected one-pixel branches survive. Under the agreed one-main-object
    scope, tiny detached components without a stable interior are disposable.
    """
    y,x=np.nonzero(body)
    if not len(y):return body,dict(discarded_components=0,discarded_pixels=0)
    y0,y1=int(y.min()),int(y.max())+1;x0,x1=int(x.min()),int(x.max())+1
    local=body[y0:y1,x0:x1];core=local.copy()
    for _ in range(3):
        padded=np.pad(core,1,mode='constant');core=np.ones_like(core)
        for dy in range(3):
            for dx in range(3):core &= padded[dy:dy+len(local),dx:dx+local.shape[1]]
    groups=components(local,local,driver)
    keep=[]
    for item in groups:
        keep.append(any(core[y,a:b].any() for y,a,b in item['runs']))
    if not any(keep):
        keep[int(np.argmax([v['area'] for v in groups]))]=True
    result=body.copy();removed=0;count=0
    for item,take in zip(groups,keep):
        if take:continue
        removed+=item['area'];count+=1
        for y,a,b in item['runs']:result[y+y0,a+x0:b+x0]=False
    return result,dict(discarded_components=count,discarded_pixels=removed,
                        assumption='one_main_object_small_detached_fragments_ignored',
                        connected_thin_branches_preserved=True,
                        erosion_used_for_markers_only=True)


def _process(driver,device,frame,reference,ground,noise,progress,sink,axis_col=None):
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    import roi_reference
    start=time.monotonic();parameter_started=start
    validity,exterior_info=exterior_parameter_mask(frame,reference,ground,driver)
    cfg=driver.prepare_parameters(frame,reference,ground,noise,exterior_validity=validity)
    cfg,rim_info=validate_exterior_parameters(frame,reference,ground,noise,cfg,driver)
    parameter_s=time.monotonic()-parameter_started
    records=sink['records'];bottom=int(np.floor(ground))
    def classify(ref,parameters,kind):
        record=dict(reference=ref,parameters=parameters,kind=kind)
        records.append(record)
        a,b,st,wp=device.classify(frame,ref,parameters)
        record.update(status=st,packed=wp)
        sink.update(parameters=parameters,strong=a,possible=b,packed=wp,modeled_reference=ref,status=st)
        return a,b,st,wp
    if progress:progress.update('PL测量框主体判定')
    s,p,st,wp=classify(reference,cfg,'original_reference_colour')
    baseline=wp.copy();base_cfg=cfg
    cleanup_started=time.monotonic()
    seedless=False
    try:body,cleanup=fast_binary(s,p,ground,driver,axis_col)
    except NoStrongSeed:
        seedless=True
        body=np.zeros((480,800),bool)
        cleanup=dict(_main_core=np.zeros((bottom,width),bool),needs_partition=True,
            reason='initial_strong_seed_missing_review_required')
    protected=np.zeros((480,800),bool)
    protected[:bottom,left:right]=cleanup.pop('_main_core')
    cleanup_s=time.monotonic()-cleanup_started
    parameter_started=time.monotonic()
    trend=background_trend(frame,reference,cfg,ground,driver)
    # An unrelated exterior trend is diagnostic, not a reason to refit a
    # confident object. Review only when it can hide material in the body.
    ref_bins=driver._rgb(reference[::8,::4])>>4
    sampled_core=protected[::8,::4]
    ratio=np.stack([(65536./cfg['inverse_variance'][c,ref_bins[...,c]])/[64.,16.,64.][c]
                    for c in range(3)],axis=-1)
    noise_inflation=(float(np.mean((ratio[sampled_core]>1.5).sum(axis=1)>=2))
                     if sampled_core.any() else 0.)
    force_rim_review=bool(rim_info['requires_spatial_review'])
    if force_rim_review:protected[:]=False
    parameter_s+=time.monotonic()-parameter_started
    registration=dict(accepted=False,position_yx=[0.,0.],reason='not_needed')
    fit=dict(applied=False,reason='main_body_passed_fast_binary_ownership')
    grid=np.zeros((30,50,3),np.float32);model=reference
    route='single_PL_fixed_window_binary_ownership'
    graph_info=None;review_s=0.;feature_records=[];family_review=None
    # Large weak extensions require measured source edges, not a tight crop
    # or an unsupported "everything beyond the core box is noise" rule.
    measured_box=[width,bottom,0,0]
    for item in components((s&body)[:bottom,left:right],(s&body)[:bottom,left:right],driver):
        if not any(protected[y,x0+left:x1+left].any() for y,x0,x1 in item['runs']):continue
        a,b,c,d=item['box'];measured_box=[min(measured_box[0],a),min(measured_box[1],b),max(measured_box[2],c),max(measured_box[3],d)]
    support=np.zeros((bottom,width),bool)
    a,b,c,d=measured_box
    if c>a and d>b:support[max(0,b-2):min(bottom,d+2),max(0,a-2):min(width,c+2)]=True
    weak_extension=(body&~s)[:bottom,left:right]&~support
    extension_pixels=int(weak_extension.sum())
    large_weak_extension=bool(extension_pixels>max(32,int(body.sum()*.03)))
    if large_weak_extension and noise_inflation<=.1 and not force_rim_review:
        body,graph_info,feature_records=partition(device,frame,reference,cfg,ground,noise,
            driver,progress,s,p,wp,st,evidence_sink=sink,axis_col=axis_col,force_confidence=True)
        body |= protected
        cleanup=dict(graph_info['selection'],candidate_touches_window=bool(
            body[0,left:right].any() or body[:bottom,left].any() or body[:bottom,right-1].any()),
            binary_competition_iterations=0,unresolved_candidate_pixels=0,hole_filling=False,outline_expansion=False)
        cleanup_s+=graph_info['elapsed_host_or_board_s'];packed=wp
        route='main_weak_extensions_verified_by_bounded_PL_edges'
    elif force_rim_review or cleanup['needs_partition'] or (trend['requires_review'] and noise_inflation>.1):
        if progress:progress.update('一次稀疏背景位置与光照校正')
        review_started=time.monotonic()
        family_review=reference_family_explains_rim(frame,reference,cfg,ground,noise,driver)
        if family_review['accepted']:
            model=reference
            fit=dict(applied=False,reason='sparse_window_background_explained_by_cardinal_reference_family')
        else:
            aligned,registration=register_background(frame,reference,cfg,ground,driver)
            model,grid,fit=roi_reference.fit_reference(frame,aligned,cfg,ground)
        review_s=time.monotonic()-review_started;parameter_s+=review_s
        if family_review['accepted'] or fit['applied'] or registration['accepted']:
            if not family_review['accepted']:
                parameter_started=time.monotonic()
                cfg=driver.prepare_parameters(frame,model,ground,noise,
                    background_samples=roi_reference.verified_samples(fit),photometric_parameters=cfg)
                parameter_s+=time.monotonic()-parameter_started
            # One bounded family replaces the old nested 19/28/31-pass routes.
            # Cardinal positions model measured one-pixel background uncertainty;
            # they do not dilate a silhouette or modify a source photograph.
            positions=((0,0),(-1,0),(1,0),(0,-1),(0,1))
            padded=np.pad(model,1,mode='edge');s=p=None
            for j,(dy,dx) in enumerate(positions):
                if progress:progress.update('PL主体参考核验','%d/5'%(j+1))
                shifted=padded[1+dy:481+dy,1+dx:801+dx]
                grouped=dict(cfg,_within_reference_group=j>0)
                kind=('original_colour_offset_' if model is reference else 'modeled_colour_offset_')+'%d_%d'%(dy,dx)
                a,b,st,wp=classify(shifted,grouped,kind)
                s=a if s is None else s&a;p=b if p is None else p&b
            cleanup_started=time.monotonic()
            body,cleanup=fast_binary(s,p,ground,driver,axis_col)
            cleanup.pop('_main_core',None)
            # Protect only the original main CORE, not all noisy connected bits.
            # These are measured PL strong pixels, never filled negative rays.
            restored=protected&~body
            body |= protected
            cleanup_s+=time.monotonic()-cleanup_started
            s=(s|protected)&body
            packed=driver._pack_decisions(s,p|protected)
            cleanup['retained_original_main_core_pixels']=int(protected.sum())
            cleanup['original_main_core_pixels_restored']=int(restored.sum())
            route=('validated_original_cardinal_reference_family_binary_ownership' if family_review['accepted'] else
                   'one_background_model_cardinal_PL_family_then_binary_ownership')
        else:
            # A single bounded source-region alternative for scenes whose
            # uncertainty cannot be described by the light/position model.
            cfg=dict(base_cfg,
                inverse_variance=np.tile(np.asarray([1024,4096,1024],np.uint16)[:,None],(1,16)),
                low=int(np.ceil(7.815*65536)),high=int(np.ceil(12.838*65536)))
            if progress:progress.update('PL主体颜色与边缘核验')
            s,p,st,wp=classify(reference,cfg,'source_region_colour')
            body,graph_info,feature_records=partition(device,frame,reference,cfg,ground,noise,
                driver,progress,s,p,wp,st,evidence_sink=sink,axis_col=axis_col)
            cleanup=dict(graph_info['selection'],candidate_touches_window=bool(
                body[0,left:right].any() or body[:bottom,left].any() or body[:bottom,right-1].any()),
                binary_competition_iterations=0,unresolved_candidate_pixels=0,
                hole_filling=False,outline_expansion=False)
            cleanup_s+=graph_info['elapsed_host_or_board_s']
            packed=wp
            route='bounded_fixed_window_source_regions'
    else:
        packed=wp
    cleanup_started=time.monotonic()
    body,speckle_info=discard_unattached_speckles(body,driver)
    cleanup_s+=time.monotonic()-cleanup_started
    # Diagnostics describe exactly the supported measurement domain. A window
    # contact is recorded; it is no longer a background-induced crop exception.
    import interior_evidence,competitive_colour
    recovery_baseline=body.copy()
    recovery_started=time.monotonic()
    body,recovered,recovery_unknown,recovery_info=competitive_colour.recover(
        device,frame,model,cfg,ground,noise,body,s,p,driver,progress,sink,axis_col)
    recovery_info['elapsed_s']=time.monotonic()-recovery_started
    if progress:progress.update('内部负证据核验')
    interior=interior_evidence.audit(body,frame,model,cfg,noise,(left,0,right,bottom),driver)
    # Undecided low-contrast material is not a measured PL foreground bit.
    # Positive texture evidence of real background remains a veto.
    recovery_unknown &= ~body & ~interior['background'] & ~interior['inferred']
    interior['unknown'] |= recovery_unknown
    interior['conservative'] = body | interior['unknown'] | interior['inferred']
    interior['diagnostics'].update(
        unknown_pixels=int(interior['unknown'].sum()),
        low_contrast_unknown_pixels=int(recovery_unknown.sum()),
        low_contrast_recovery_version=66,
        unknown_scope='enclosed_negatives_and_PL_supported_open_low_contrast_regions',
        not_measured_material_probability=True)
    if progress:progress.update('复用PL证据核验薄部件与低对比区域')
    evidence_started=time.monotonic()
    evidence_foreground,evidence_unknown,evidence_info=subject_evidence.review(frame,model,cfg,noise,ground,
        body,interior['conservative'],interior['background'],baseline,records,recovery_info,
        driver,axis_col,source_reference=reference,foreground_output=True)
    evidence_review_s=time.monotonic()-evidence_started
    before_region_body_sha256=hashlib.sha256(body.astype(np.uint8).tobytes()).hexdigest()
    evidence_unknown &= ~interior['conservative']
    evidence_info['added_uncertainty_pixels']=int(evidence_unknown.sum())
    body=body|evidence_foreground
    interior['unknown']=(interior['unknown']|evidence_unknown)&~body
    interior['inferred'] &= ~body
    # The schema hash describes the base upper mask before NEW unknown bits;
    # confirmed regional foreground is now part of the actual stage-7 mask.
    evidence_info['source_upper_sha256']=hashlib.sha256(
        np.asarray(interior['conservative']|evidence_foreground,np.uint8).tobytes()).hexdigest()
    interior['conservative']=body|interior['unknown']|interior['inferred']
    interior['diagnostics'].update(unknown_pixels=int(interior['unknown'].sum()),
        source_evidence_unknown_pixels=int(evidence_unknown.sum()),source_evidence_version=75)
    status=[r['status'] for r in records if 'status' in r]
    info=dict(cfg.get('info',{}),**cleanup)
    info.update(version=76,algorithm='PL_fixed_window_region_foreground_v76',backend='PL',
        processing_route=route,roi_xyxy=[left,0,right,480],measurement_window_xyxy=[left,0,right,bottom],
        turntable_axis_hint_col=None if axis_col is None else float(axis_col),
        assumed_full_rotation_inside_window=True,physical_object_completeness_certified=False,
        outside_window_object_search=False,outside_window_component_search=False,
        outside_window_used_only_for_sparse_nuisance_estimation=True,
        full_resolution_pixel_comparison_backend='PL',final_binary_ownership_backend='PS',
        binary_cleanup='fixed_window_seeded_competition',strong_noise_cluster_cutoff=6,
        boundary_requires_review=False,boundary_blocking_components=[],boundary_removed_components=[],
        boundary_removed_pixels=0,frame_raw_color_modified=False,hole_filling=False,geometric_expansion=False,
        background_appearance_model=fit,background_registration=registration,
        reference_family_rim_validation=family_review,
        sparse_exterior_trend=trend,body_reference_noise_inflation_fraction=noise_inflation,
        weak_extension_pixels_before_review=extension_pixels,large_weak_extension_review=large_weak_extension,
        exterior_parameter_filter=exterior_info,exterior_parameter_rim_validation=rim_info,
        source_region_refinement=graph_info,
        source_evidence_review=evidence_info,source_evidence_review_s=evidence_review_s,
        before_region_body_sha256=before_region_body_sha256,
        low_contrast_recovery=recovery_info,initial_strong_seed_missing=seedless,detached_fragment_cleanup=speckle_info,interior_evidence=interior["diagnostics"],
        pl_total_passes=len(status),pl_reference_passes=len(status),max_total_PL_passes=17,
        maximum_texture_patch_centers=256,maximum_binary_competition_steps=64,
        pl_reference_pass_status=status,
        pl_dma_transfer_and_wait_s=sum(v.get('pl_dma_transfer_and_wait_s',0.) for v in status),
        packing_configuration_dma_decode_s=sum(v.get('packing_configuration_dma_decode_s',0.) for v in status),
        parameter_estimation_s=parameter_s,binary_cleanup_s=cleanup_s,background_review_s=review_s,
        segmentation_elapsed_s=time.monotonic()-start,frame_sha256=_hash(frame),
        pl_packed_decisions_kind='PL_binary_evidence_and_declared_main_core_protection',
        high_threshold=float(np.sqrt(cfg['high']/65536.)),low_threshold=float(np.sqrt(cfg['low']/65536.)))
    if len(status)>17:raise RuntimeError('主体处理超过既定PL预算')
    result=dict(body=body,interior=interior,strong=(s&body),unknown=(body&~s),packed=packed,baseline=baseline,
                parameters=cfg,diagnostics=info,model_grid=grid,model=model,
                protected=protected,records=records,recovery_baseline=recovery_baseline,recovered=recovered,recovery_unknown=recovery_unknown,
                evidence_unknown=evidence_unknown,evidence_foreground=evidence_foreground)
    return subject_evidence.apply_main_component_policy(result)


def segment_frames(driver,device,frames,reference,ground,noise=None,directory=None,progress=None,label='照片',axis_col=None):
    left, _, right, _ = getattr(driver, "measurement_roi", (160,0,640,480))
    width=right-left
    import roi_reference
    started=time.monotonic();reference=driver._frame(reference)
    frames=np.asarray(frames)
    if frames.dtype!=np.uint16 or frames.ndim!=3 or frames.shape[1:]!=(480,800) or not len(frames):
        raise ValueError('需要非空的480x800 uint16彩色帧序列')
    if not np.isfinite(ground) or not 8<ground<=480:raise ValueError('台面行无效')
    if axis_col is not None and (not np.isfinite(axis_col) or not left<=axis_col<right):
        raise ValueError('标定转轴不在固定测量框内')
    if noise is not None:
        noise=np.asarray(noise)
        if noise.shape!=(480,800,3) or not np.isfinite(noise).all() or (noise<0).any():
            raise ValueError('背景噪声数据无效')
    results=[];pass_frames=[];pass_records=[];modeled_indices=[];models=[]
    material_tables=[] if hasattr(subject_evidence,'compact_material_diagnostics') else None
    for i,frame in enumerate(frames):
        if progress:progress.update('处理主体','%s %d/%d'%(label,i+1,len(frames)))
        sink=dict(records=[])
        try:
            result=_process(driver,device,frame,reference,ground,noise,progress,sink,axis_col)
        except (ValueError,RuntimeError,np.linalg.LinAlgError) as exc:
            completed=[r for r in sink['records'] if 'packed' in r]
            actual=sink.get('parameters');evidence=dict(
                completed_actual_PL_packed=np.asarray([r['packed'] for r in completed],np.uint32).reshape(-1,driver.WORDS),
                completed_reference_sha256=np.asarray([(r.get('reference_sha256') or _hash(r['reference'])) for r in completed]),
                completed_reference_constant565=np.asarray([r.get('constant_reference565',-1) for r in completed],np.int32),
                completed_actual_PL_kind=np.asarray([r['kind'] for r in completed]),
                completed_actual_PL_luts=np.asarray([r['parameters']['lut'] for r in completed],np.uint8).reshape(-1,3,256),
                completed_actual_PL_inverse_variances=np.asarray([r['parameters']['inverse_variance'] for r in completed],np.uint16).reshape(-1,3,16),
                completed_actual_PL_low_q16=np.asarray([r['parameters']['low'] for r in completed],np.uint32),
                completed_actual_PL_high_q16=np.asarray([r['parameters']['high'] for r in completed],np.uint32),
                diagnostics=dict(version=76,measurement_window_xyxy=[left,0,right,int(ground)],
                                 actual_PL_completed_passes=len(completed)))
            try:
                details=driver._save_failure(directory,i+1,frame,reference,ground,exc,actual,
                    sink.get('possible'),sink.get('strong'),sink.get('status'),sink.get('packed'),noise,
                    sink.get('modeled_reference'),baseline_evidence=evidence)
            except Exception as save_error:details='；诊断保存失败：%s'%save_error
            raise RuntimeError('第%d张主体处理失败：%s%s'%(i+1,exc,details)) from exc
        if material_tables is not None:
            material_tables.append(subject_evidence.compact_material_diagnostics(result['diagnostics'],i))
        results.append(result)
        if directory is not None:
            # Tiny timing checkpoint survives a later frame failure. No raw
            # image compression or repeated per-pixel snapshots are added.
            target=Path(directory)/'1023_subject_progress_v66.json'
            target.parent.mkdir(parents=True,exist_ok=True)
            rows=[dict(frame=j+1,elapsed_s=r['diagnostics']['segmentation_elapsed_s'],
                parameter_s=r['diagnostics']['parameter_estimation_s'],
                binary_cleanup_s=r['diagnostics']['binary_cleanup_s'],
                actual_PL_passes=r['diagnostics']['pl_total_passes'],
                DMA_wait_s=r['diagnostics']['pl_dma_transfer_and_wait_s'],
                packing_and_DMA_s=r['diagnostics']['packing_configuration_dma_decode_s'],
                route=r['diagnostics']['processing_route'],
                material_review_s=r['diagnostics']['source_evidence_review'].get('source_region_material_review',{}).get('elapsed_s',0.)) for j,r in enumerate(results)]
            temporary=target.with_suffix('.tmp')
            try:
                temporary.write_text(json.dumps(dict(version=66,completed=len(rows),total=len(frames),frames=rows),ensure_ascii=False,indent=2),encoding='utf8')
                temporary.replace(target)
            except OSError as error:
                result['diagnostics']['progress_checkpoint_error']=str(error)
        for r in result['records']:
            pass_frames.append(i);pass_records.append(r)
        if result['model'] is not reference:
            modeled_indices.append(i);models.append(result['model'])
    output=dict(region_foreground_version=76,
        region_foreground_packed=np.packbits(np.stack([r['evidence_foreground'] for r in results]).reshape(len(results),-1),axis=1),
        region_foreground_shape_yx=np.asarray([480,800],np.int16),
        source_evidence_unknown_masks=np.stack([r['evidence_unknown'] for r in results]).astype(np.uint8),
        source_evidence_version=75,
        source_evidence_base_upper_sha256=np.asarray([r['diagnostics']['source_evidence_review']['source_upper_sha256'] for r in results]),
        low_contrast_baseline_masks=np.stack([r['recovery_baseline'] for r in results]).astype(np.uint8),
        low_contrast_recovered_masks=np.stack([r['recovered'] for r in results]).astype(np.uint8),
        low_contrast_unknown_masks=np.stack([r['recovery_unknown'] for r in results]).astype(np.uint8),
        low_contrast_recovery_version=66,
        interior_unknown_masks=np.stack([r['interior']['unknown'] for r in results]).astype(np.uint8),
        interior_inferred_masks=np.stack([r['interior']['inferred'] for r in results]).astype(np.uint8),
        interior_confirmed_background_masks=np.stack([r['interior']['background'] for r in results]).astype(np.uint8),
        conservative_surface_masks=np.stack([r['interior']['conservative'] for r in results]).astype(np.uint8),
        reconstruction_roi_xyxy=np.asarray(__import__('interior_evidence').reconstruction_roi((left,0,right,int(ground))),np.int16),
        interior_evidence_version=53,
        masks=np.stack([r['body'] for r in results]).astype(np.uint8),
        photo_strong_masks=np.stack([r['strong'] for r in results]).astype(np.uint8),
        photo_unknown_masks=np.stack([r['unknown'] for r in results]).astype(np.uint8),
        pl_packed_decisions=np.stack([r['packed'] for r in results]),
        pl_baseline_packed_decisions=np.stack([r['baseline'] for r in results]),
        pl_luts=np.stack([r['parameters']['lut'] for r in results]),
        pl_inverse_variances=np.stack([r['parameters']['inverse_variance'] for r in results]),
        pl_low_q16=np.asarray([r['parameters']['low'] for r in results],np.uint32),
        pl_high_q16=np.asarray([r['parameters']['high'] for r in results],np.uint32),
        background_model_grids=np.stack([r['model_grid'] for r in results]),background_model_version=53,
        photo_thresholds=np.asarray([r['diagnostics']['high_threshold'] for r in results]),
        photo_threshold_units='standardized_background_residual',
        segmentation_diagnostics_json=json.dumps([r['diagnostics'] for r in results],ensure_ascii=False,separators=(',',':')),
        segmentation_algorithm='PL_fixed_window_region_foreground_v76',segmentation_backend='PL',
        segmentation_version=8,segmentation_implementation_version=20,subject_window_version=53,subject_recovery_version=66,
        pl_packed_decisions_kind='PL_binary_evidence_and_declared_main_core_protection',
        segmentation_elapsed_s=time.monotonic()-started,
        measurement_window_xyxy=np.asarray([left,0,right,int(ground)],np.int16),
        measurement_scope_json=json.dumps(dict(version=53,one_main_object=True,
            complete_rotation_assumed_inside_window=True,exterior_changes_not_objects=True,
            physical_completeness_verified=False),ensure_ascii=False),
        pl_actual_pass_frame_indices=np.asarray(pass_frames,np.int16),
        pl_actual_pass_packed=np.stack([r['packed'] for r in pass_records]),
        pl_actual_pass_luts=np.stack([r['parameters']['lut'] for r in pass_records]),
        pl_actual_pass_inverse_variances=np.stack([r['parameters']['inverse_variance'] for r in pass_records]),
        pl_actual_pass_low_q16=np.asarray([r['parameters']['low'] for r in pass_records],np.uint32),
        pl_actual_pass_high_q16=np.asarray([r['parameters']['high'] for r in pass_records],np.uint32),
        pl_actual_pass_reference_sha256=np.asarray([(r.get('reference_sha256') or _hash(r['reference'])) for r in pass_records]),
        pl_actual_pass_reference_constant565=np.asarray([r.get('constant_reference565',-1) for r in pass_records],np.int32),
        pl_actual_pass_kind=np.asarray([r['kind'] for r in pass_records]),
        roi_modeled_reference_frame_indices=np.asarray(modeled_indices,np.int16),
        roi_modeled_references565=np.asarray(models,np.uint16).reshape(-1,480,800),
        main_core_protection_masks=np.stack([r['protected'] for r in results]).astype(np.uint8))
    if material_tables is not None:
        output.update(subject_evidence.material_diagnostic_arrays(material_tables))
        output['source_material_review_elapsed_s']=np.asarray([r['diagnostics']['source_evidence_review'].get('source_region_material_review',{}).get('elapsed_s',0.) for r in results],np.float64)
    output.update(single_component_filter_version=np.int32(81),single_component_filter_connectivity=np.int32(8),
        component_filter_original_masks_packed=np.stack([r['component_original_body_packed'] for r in results]),
        component_filter_original_upper_packed=np.stack([r['component_original_upper_packed'] for r in results]),
        single_component_filter_mode='strict_largest_8_connected_component')
    return output
