"""Sparse subpixel background-edge registration; never emits an object mask.

PS fits background margins and renders RGB565 references. All full-resolution
foreground comparisons still run on the PL core. No source pixel is warped.
"""
import hashlib
import time

import numpy as np

IMPLEMENTATION_VERSION = 39
SHAPE = (480,800)


def rendered_parameters(parameters):
    """Account for the bounded rounding error of a predicted RGB565 reference.

    This variance applies only where the reference is rendered. The caller
    keeps original PL evidence everywhere else; no global threshold is relaxed.
    """
    variance=65536./np.asarray(parameters['inverse_variance'],dtype=float)
    # Compute the actual pack-nearest / bit-replicating decoder error bound.
    # A uniform-random q^2/12 assumption would underestimate this deterministic
    # interpolation-and-rounding uncertainty, especially at a sharp edge.
    bounds=[]
    for bits in (5,6,5):
        codes=np.arange(1<<bits)
        decoded=((codes<<(8-bits)) | (codes>>(2*bits-8))).astype(float)
        step=255./((1<<bits)-1)
        lower=np.maximum(0.,(codes-.5)*step)
        upper=np.minimum(255.,(codes+.5)*step)
        bounds.append(float(max(np.abs(decoded-lower).max(),np.abs(decoded-upper).max())))
    extra=(np.asarray(bounds)**2)[:,None]
    inverse=np.clip(np.rint(65536./(variance+extra)),1,65535).astype(np.uint16)
    info=dict(parameters.get('info',{}))
    info.update(rendered_reference_rounding_error_bound_rgb=bounds,
                rendered_reference_rounding_error_bound_square_rgb=extra[:,0].tolist(),
                rendered_reference_variance_is_local=True,
                variance_by_reference_bin=(65536./inverse.astype(float)).tolist())
    return dict(parameters,inverse_variance=inverse,info=info)


def _rgb(a):
    a=np.asarray(a,dtype=np.uint16)
    r,g,b=(a>>11)&31,(a>>5)&63,a&31
    return np.stack(((r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)),axis=-1).astype(float)


def _pack(rgb):
    a=np.clip(rgb,0,255)
    r=np.rint(a[...,0]*31/255.).astype(np.uint16)
    g=np.rint(a[...,1]*63/255.).astype(np.uint16)
    b=np.rint(a[...,2]*31/255.).astype(np.uint16)
    return (r<<11)|(g<<5)|b


def _weight(y,low,high,start,stop):
    return np.minimum(np.clip((y-start)/max(1.,low-start),0,1),
                      np.clip((stop-1-y)/max(1.,stop-1-high),0,1))


def _interpolate_rows(strip,position,x):
    """Interpolate decoded RGB, within the adjacent reference-colour segment."""
    a=np.floor(position).astype(int);fraction=(position-a)[...,None]
    return (strip[np.clip(a,0,len(strip)-1),x]*(1-fraction)+
            strip[np.clip(a+1,0,len(strip)-1),x]*fraction)


def _samples(reference, y, x, low, high, start, stop):
    yy=np.arange(max(0,start-2),min(480,stop+2))
    strip=_rgb(reference[yy])
    origin=int(yy[0]);w=_weight(y,low,high,start,stop)
    zero=strip[y-origin,x]
    gradient=strip[y-origin+1,x]-strip[y-origin-1,x]
    edge=(np.abs(gradient)>=[16,8,16]).any(axis=1)
    return strip,origin,w,zero,edge


def _profile(frame,parameters,y,x,strip,origin,weight,edge):
    parameters=rendered_parameters(parameters)
    current=_rgb(frame[y,x]).astype(int)
    observed=np.stack([parameters['lut'][c,current[:,c]] for c in range(3)],axis=-1)
    positions=np.arange(-16,17,dtype=float)/16.
    losses=[]
    for dy in positions:
        v=y+dy*weight-origin
        expected=_interpolate_rows(strip,v,x)
        # Fit the same quantized reference representation sent to the PL.
        expected=_rgb(_pack(expected))
        variance=65536./np.stack([parameters['inverse_variance'][c,
            expected[:,c].astype(int)>>4] for c in range(3)],axis=-1)
        score=np.sum((observed-expected)**2/variance,axis=1)
        losses.append(np.minimum(score,40.))
    return positions,np.asarray(losses),((x//2+y)%2),edge


def propose(frame,reference,parameters,ground_row,foreground,
            context_frames=None,frame_index=0,roi=(160,0,640,480)):
    """A <=1-pixel vertical nuisance proposal, supported by background only.

    This deliberately abstains on broad boundary objects, missing edge texture,
    inconsistent coordinate folds, or unconfirmed temporal background evidence.
    """
    started=time.monotonic()
    x0,y0,x1,y1=map(int,roi);bottom=min(y1,int(np.floor(ground_row)))
    active=np.asarray(foreground,dtype=bool)
    left=np.flatnonzero(active[y0:bottom,x0])+y0
    right=np.flatnonzero(active[y0:bottom,x1-1])+y0
    info=dict(version=39,accepted=False,reference_only=True,
              source_image_modified=False,reference_radius_px=1.,
              object_class_prior=False,hole_filling=False,
              background_samples_are_assumed_outside_roi=True)
    def refuse(reason):
        info.update(reason=reason,elapsed_host_or_board_s=time.monotonic()-started)
        return None,info
    if active[y0,x0:x1].any():
        return refuse('top_boundary_requires_other_evidence')
    rows=np.unique(np.r_[left,right])
    if not len(rows) or rows[-1]-rows[0]>8:
        return refuse('not_a_compact_side_boundary_band')
    low,high=int(rows[0]),int(rows[-1]);start=max(1,low-4);stop=min(479,high+5)
    if low-start<2 or stop-1-high<2:
        return refuse('insufficient_image_margin')
    xs=[]
    if len(left):xs.extend(range(4,x0,2))
    if len(right):xs.extend(range(x1,796,2))
    if not xs:return refuse('no_exterior_background_samples')
    yy,xx=np.meshgrid(np.arange(start,stop),np.asarray(xs),indexing='ij')
    y,x=yy.ravel(),xx.ravel()
    strip,origin,weight,zero,edge=_samples(reference,y,x,low,high,start,stop)
    positions,loss,phase,edge=_profile(frame,parameters,y,x,strip,origin,weight,edge)
    valid=np.ones(len(positions),bool);folds=[];zero_index=16
    for k in (0,1):
        hit=edge&(phase==k);n=int(hit.sum())
        if n<8:return refuse('insufficient_edge_samples_in_coordinate_fold')
        data=loss[:,hit];mean=data.mean(axis=1);best=int(np.argmin(mean))
        sem=float(data[best].std()/np.sqrt(n))
        improvement=float(mean[zero_index]-mean[best])
        folds.append(dict(samples=n,baseline_loss=float(mean[zero_index]),
                          best_loss=float(mean[best]),best_dy=float(positions[best]),
                          noise_loss_sem=sem,improvement=improvement))
        if mean[best]>7.815 or improvement<max(1.,.20*mean[zero_index]):
            info['folds']=folds
            return refuse('no_heldout_background_edge_improvement')
        valid &= mean <= mean[best]+sem
    poses=positions[valid]
    info.update(folds=folds,sparse_sample_pixels=len(y),
                edge_samples=int(edge.sum()),row_support=[start,stop],
                trigger_rows=[low,high])
    if not len(poses) or poses[-1]-poses[0]>.5 or len(poses)>9 or np.any(poses==0):
        return refuse('subpixel_position_not_identified')
    # Odd columns were not used to select the model. Validate them on BOTH
    # background sides so a fit on one margin cannot silently spoil the other.
    hold_x=np.r_[np.arange(5,x0,2),np.arange(x1+1,796,2)]
    hy,hx=np.meshgrid(np.arange(start,stop),hold_x,indexing='ij')
    hy,hx=hy.ravel(),hx.ravel()
    hs,ho,hw,hzero,hedge=_samples(reference,hy,hx,low,high,start,stop)
    _,hloss,_,_=_profile(frame,parameters,hy,hx,hs,ho,hw,hedge)
    pose_indices=np.flatnonzero(valid)
    hbest=hloss[pose_indices].min(axis=0)
    holdout=[]
    for name,hit,fitted in [('left',hx<x0,bool(len(left))),('right',hx>=x1,bool(len(right)))]:
        tested=hit&hedge
        n=int(tested.sum())
        before=float(hloss[zero_index,hit].mean())
        after=float(hbest[hit].mean())
        if after>before+1.:
            return refuse('unused_background_columns_worsened')
        if fitted:
            if n<8:return refuse('insufficient_unused_background_edges')
            edge_before=float(hloss[zero_index,tested].mean())
            edge_after=float(hbest[tested].mean())
            if edge_after>7.815 or edge_before-edge_after<max(1.,.20*edge_before):
                return refuse('unused_background_edges_do_not_confirm_model')
        holdout.append(dict(side=name,samples=int(hit.sum()),edge_samples=n,
                            baseline_loss=before,model_loss=after))
    info['unused_column_validation']=holdout
    # On scans, confirm this reference edge is also visible at other times.
    # This is background-template validation, not cross-view object filling.
    witnesses=[]
    if context_frames is not None and len(context_frames)>=8:
        n=len(context_frames)
        indices=sorted(set((frame_index+step)%n for step in
            (-max(2,n//4),-max(2,n//8),max(2,n//8),max(2,n//4))))
        seen=set()
        for i in indices:
            # A genuinely static background should repeat. Deduplicate entire
            # photos, not the background patch used to verify that repetition.
            signature=hashlib.sha256(np.asarray(context_frames[i]).astype('<u2',copy=False).tobytes()).hexdigest()
            if signature in seen:continue
            seen.add(signature)
            _,other,other_phase,_=_profile(context_frames[i],parameters,y,x,strip,origin,weight,edge)
            values=[]
            for k in (0,1):
                values.append(float(np.min(other[:,edge&(other_phase==k)].mean(axis=1))))
            witnesses.append(dict(frame_index=int(i),best_fold_losses=values,
                                  accepted=bool(max(values)<=7.815)))
        if sum(v['accepted'] for v in witnesses)<2:
            info['temporal_witnesses']=witnesses
            return refuse('reference_edge_not_confirmed_in_other_views')
    info.update(accepted=True,reason='subpixel_background_edge_supported',
                dy_hypotheses=poses.tolist(),temporal_witnesses=witnesses,
                temporal_confirmation_required=context_frames is not None and len(context_frames)>=8,
                thresholds_modified=False,elapsed_host_or_board_s=time.monotonic()-started)
    proposal=dict(poses=poses,low=low,high=high,start=start,stop=stop)
    return proposal,info


def render_reference(reference,proposal,dy):
    """Render only a narrow reference strip; never resample the source photo."""
    start,stop=proposal['start'],proposal['stop']
    first=max(0,start-2);last=min(480,stop+2)
    strip=_rgb(reference[first:last])
    y,x=np.mgrid[start:stop,:800]
    weight=_weight(y,proposal['low'],proposal['high'],start,stop)
    position=y+float(dy)*weight-first
    rgb=_interpolate_rows(strip,position,x)
    result=np.asarray(reference).copy()
    result[start:stop]=_pack(rgb)
    return result
