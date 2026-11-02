"""Sparse three-view surface evidence, v35; never a full PS segmentation.

Original PL masks/volume remain untouched. A zero pixel can support a localized
surface correction only with independent background rejection, distinctive
photo correspondence on both neighboring sides, and a bounded depth interval.
Unknowns and true background observations are not automatically filled.
"""
import time
import numpy as np

VERSION=35


def rgb565(a):
    a=np.asarray(a);r=(a>>11)&31;g=(a>>5)&63;b=a&31
    return np.stack(((r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)),axis=-1).astype(np.float32)


def internal_zero_coordinates(mask):
    from pl_mask import _regions
    runs,labels,_,_=_regions(~mask,np.zeros_like(mask))
    h,w=mask.shape
    edge={int(labels[k]) for y,a,b,k in runs if y in (0,h-1) or a==0 or b==w}
    coords=[(y,x) for y,a,b,k in runs if int(labels[k]) not in edge for x in range(a,b)]
    return np.asarray(coords,dtype=np.intp).reshape(-1,2)


def background_test(frame,reference,sigma,parameters,coordinates,ground):
    """Sparse RGB covariance model, calibrated against held-out real background."""
    yy,xx=np.mgrid[:frame.shape[0]:8,:frame.shape[1]:4]
    left,_,right,_=parameters.get('measurement_roi',(160,0,640,480))
    anchor=(yy<ground)&((xx<left)|(xx>=right))
    cur=rgb565(frame[::8,::4]);ref=rgb565(reference[::8,::4]);lut=parameters['lut']
    obs=np.stack([lut[c,cur[...,c].astype(int)] for c in range(3)],axis=-1)
    residual=(obs[anchor]-ref[anchor]).astype(float)
    if len(residual)<512:return np.zeros(len(coordinates),bool),dict(reason='background_samples_insufficient')
    train=np.arange(len(residual))%2==0;fit=residual[train]
    center=np.median(fit,axis=0)
    scale=np.maximum(np.median(np.abs(fit-center),axis=0),[3.,2.,3.])
    fit=fit[np.max(np.abs(fit-center)/scale,axis=1)<5]
    covariance=np.cov((fit-center).T)+np.diag([64.,16.,64.])/12.
    inverse=np.linalg.inv(covariance)
    test=residual[~train]-center
    scores=np.einsum('ij,jk,ik->i',test,inverse,test)
    threshold=max(12.838,float(np.percentile(scores,99.5)))
    info=dict(threshold=threshold,heldout_background_samples=len(test),
        heldout_false_rejection_rate=float(np.mean(scores>threshold)),covariance=covariance.tolist(),
        evidence_is_model_conditional_not_physical_proof=True)
    if not len(coordinates):
        info['candidate_scores']=[]
        return np.zeros(0,bool),info
    y,x=coordinates.T;observed=rgb565(frame[y,x])
    observed=np.stack([lut[c,observed[:,c].astype(int)] for c in range(3)],axis=-1).astype(float)
    # Measured temporal noise is additional uncertainty, never zeroed out.
    pixel_cov=covariance[None,:,:]+np.eye(3)[None,:,:]*np.asarray(sigma[y,x],float)[:,None,:]**2
    pixel_inverse=np.linalg.inv(pixel_cov)
    minimum=np.full(len(y),np.inf)
    for dy in (-1,0,1):
        for dx in (-1,0,1):
            ref=rgb565(reference[np.clip(y+dy,0,frame.shape[0]-1),np.clip(x+dx,0,frame.shape[1]-1)])
            diff=observed-ref-center
            minimum=np.minimum(minimum,np.einsum('ij,ijk,ik->i',diff,pixel_inverse,diff))
    info['candidate_scores']=minimum.tolist()
    return minimum>threshold,info


def patches(frame,ys,xs,lut,radius=3):
    yy,xx=np.mgrid[-radius:radius+1,-radius:radius+1]
    ys=np.asarray(ys,dtype=np.intp);xs=np.asarray(xs,dtype=np.intp)
    raw=rgb565(frame[ys[:,None]+yy.ravel(),xs[:,None]+xx.ravel()])
    return np.stack([lut[c,raw[...,c].astype(int)] for c in range(3)],axis=-1).astype(np.float32)


def interpolated_patches(frame,ys,xs,lut,radius=3):
    """Sample continuous image coordinates, then voxelize the fitted depth.

    Rounding 3-D points before matching can change disparity by a whole image
    pixel and defeat subvoxel depth evidence. RGB565 words are decoded before
    interpolation; packed integer values must never be interpolated.
    """
    ys=np.asarray(ys,float);xs=np.asarray(xs,float)
    y=np.floor(ys).astype(np.intp);x=np.floor(xs).astype(np.intp)
    fy=(ys-y)[:,None,None];fx=(xs-x)[:,None,None]
    return (patches(frame,y,x,lut,radius)*(1-fx)*(1-fy)+
            patches(frame,y,x+1,lut,radius)*fx*(1-fy)+
            patches(frame,y+1,x,lut,radius)*(1-fx)*fy+
            patches(frame,y+1,x+1,lut,radius)*fx*fy)


def ncc(source,target):
    a=source-source.mean(axis=-2,keepdims=True)
    b=target-target.mean(axis=-2,keepdims=True)
    return (a*b).sum(axis=(-2,-1))/np.maximum(np.sqrt((a*a).sum(axis=(-2,-1))*(b*b).sum(axis=(-2,-1))),1e-9)


def audit_surface(occ,voxel_mm,frames565,masks,strong_masks,angles_deg,calibration,
                  reference565,reference_sigma,camera_depth_sign,segmentation_diagnostics=None):
    from ps_demo import plan_geometry
    from pl_mask import prepare_parameters
    started=time.monotonic();src=np.asarray(occ,dtype=bool)
    frames=np.asarray(frames565);sil=np.asarray(masks,dtype=bool);strong=np.asarray(strong_masks,dtype=bool)
    angles=np.asarray(angles_deg,float);sigma=np.asarray(reference_sigma)
    additions=np.zeros_like(src);evidence=[];rejections={};failed_matches=[]
    def reject(reason):rejections[reason]=rejections.get(reason,0)+1
    if (frames.dtype!=np.uint16 or frames.shape!=sil.shape or strong.shape!=sil.shape or
            frames.shape[1:]!=(480,800) or len(frames)!=len(angles) or sigma.shape!=(480,800,3)):
        raise ValueError('稀疏几何核验需要对应的彩照、PL强证据和真实背景噪声')
    if (src.ndim!=3 or len(set(src.shape))!=1 or not 8<=src.shape[0]<=256 or not src.any() or
            angles.ndim!=1 or not np.isfinite(angles).all() or len(angles)<3 or
            not np.isfinite(sigma).all() or (sigma<0).any() or
            not np.isfinite(voxel_mm) or voxel_mm<=0):
        raise ValueError('稀疏几何核验的网格、角度、噪声或采样间距无效')
    reference565=np.asarray(reference565)
    if reference565.dtype!=np.uint16 or reference565.shape!=(480,800):
        raise ValueError('稀疏几何核验需要对应空台原图')
    if segmentation_diagnostics is not None and len(segmentation_diagnostics)!=len(frames):
        raise ValueError('分割诊断与照片数不一致')
    if camera_depth_sign not in (-1,1):raise ValueError('观察方向无效')
    if segmentation_diagnostics is not None and any(
            d.get('single_component_filter',{}).get('mode')=='strict_largest_8_connected_component'
            for d in segmentation_diagnostics):
        return additions,dict(version=VERSION,added_surface_voxels=0,
            reason='user_strict_main_component_scope_no_surface_additions',
            strict_component_policy_enforced=True,masks_modified=False,PL_volume_modified=False,
            whole_rays_filled=False,elapsed_host_or_board_s=time.monotonic()-started)
    n=src.shape[0];half=(n-1)/2.;mm=float(calibration['mm_per_px']);axis=float(calibration['axis_col']);ground=float(calibration['v_ground'])
    if not np.isfinite([mm,axis,ground]).all() or mm<=0:raise ValueError('几何核验标定无效')
    kv,ofv,kx,ky,ofu=plan_geometry(angles,n,800,480,voxel_mm,mm,axis,ground)
    def project(points,j):
        xx,yy,zz=points.T;theta=np.deg2rad(angles[j])
        u=axis+((xx-half)*np.cos(theta)-(yy-half)*np.sin(theta))*voxel_mm/mm
        v=ground-zz*voxel_mm/mm
        return u,v,np.floor(u+.5).astype(np.intp),np.floor(v+.5).astype(np.intp)
    roi=tuple(calibration.get('reconstruction_roi_xyxy',(160,0,640,480)))
    params=[]
    for f in frames:
        try:cfg=prepare_parameters(f,reference565,ground,sigma,measurement_roi=roi)
        except ValueError as exc:
            if '静止背景样本不足' not in str(exc):raise
            cfg=prepare_parameters(reference565,reference565,ground,sigma)
        cfg['measurement_roi']=roi
        params.append(cfg)
    # A silhouette hull is not measured surface depth: it can contain material
    # in front of a real concavity. Do not use that hull as an occlusion oracle.
    # Require real-image bidirectional correspondence below instead.
    candidate_count=0;background_rejected=0;covariances={}
    def covariance(j):
        if j not in covariances:
            _,info=background_test(frames[j],reference565,sigma,params[j],np.empty((0,2),dtype=np.intp),ground)
            covariances[j]=np.asarray(info['covariance'])
        return covariances[j]
    for i,mask in enumerate(sil):
        coords=internal_zero_coordinates(mask);candidate_count+=len(coords)
        if not len(coords):continue
        if len(coords)>2048 or background_rejected>=512:
            reject('optional_audit_work_budget_exceeded_original_constraints_retained');continue
        interior_set={tuple(map(int,c)) for c in coords}
        if segmentation_diagnostics and segmentation_diagnostics[i].get('background_appearance_model',{}).get('applied'):
            reject('source_uses_local_reference_model_not_revalidated_here');continue
        gate,bg=background_test(frames[i],reference565,sigma,params[i],coords,ground)
        if 'covariance' not in bg:reject('background_samples_insufficient');continue
        covariances[i]=np.asarray(bg['covariance'])
        background_rejected+=int(gate.sum())
        if background_rejected>512:
            reject('optional_audit_work_budget_exceeded_original_constraints_retained');continue
        delta=(angles-angles[i]+180.)%360.-180.
        before=np.flatnonzero((delta< -5)&(delta>=-35))
        after=np.flatnonzero((delta>5)&(delta<=35))
        if not len(before) or not len(after):reject('neighbor_angles_insufficient');continue
        neighbors=[int(before[np.argmin(np.abs(delta[before]))]),int(after[np.argmin(np.abs(delta[after]))])]
        co,si=np.cos(np.deg2rad(angles[i])),np.sin(np.deg2rad(angles[i]))
        for point_index in np.flatnonzero(gate):
            v,u=map(int,coords[point_index])
            if not (4<=v<476 and 4<=u<796):reject('patch_outside_image');continue
            source=patches(frames[i],[v],[u],params[i]['lut'])[0]
            if float(np.sqrt(np.mean((source-source.mean(axis=0))**2)))<4.:
                reject('source_texture_insufficient');continue
            q=(u-axis)*mm/voxel_mm;depth=np.arange(-n*.7072,n*.7072,.5)
            xyz=np.stack((q*co+depth*si+half,-q*si+depth*co+half,np.full(len(depth),(ground-v)*mm/voxel_mm)),axis=1)
            ok=((xyz>=0)&(xyz<n)).all(axis=1);xyz=xyz[ok]
            if not len(xyz):reject('ray_outside_grid');continue
            # Each candidate must still satisfy EVERY other silhouette. This
            # is only an eligibility bound, never the foreground decision.
            for j in range(len(angles)):
                if j==i or not len(xyz):continue
                _,_,uu,vv=project(xyz,j)
                ok=(uu>=0)&(uu<800)&(vv>=0)&(vv<480);ids=np.flatnonzero(ok)
                keep=np.zeros(len(xyz),bool);keep[ids]=sil[j,vv[ids],uu[ids]];xyz=xyz[keep]
            if len(xyz)<2:reject('other_views_do_not_support_ray');continue
            xx,yy,zz=xyz.T
            scores=[];center_errors=[];center_scores=[];valids=[]
            for j in neighbors:
                uf,vf,uu,vv=project(xyz,j)
                ok=(uu>=4)&(uu<796)&(vv>=4)&(vv<476);ids=np.flatnonzero(ok)
                ok[ids]&=strong[j,vv[ids],uu[ids]]
                corr=np.full(len(xyz),-1.);err=np.full(len(xyz),np.inf);color_score=np.full(len(xyz),np.inf)
                ids=np.flatnonzero(ok)
                if len(ids):
                    samples=interpolated_patches(frames[j],vf[ids],uf[ids],params[j]['lut'])
                    corr[ids]=ncc(source[None,:,:],samples)
                    err[ids]=np.sqrt(np.mean((samples[:,24,:]-source[24])**2,axis=1))
                    # Sensor/background response uncertainty plus the finite
                    # camera-pixel footprint propagated through image gradients.
                    # Avoid an object-specific fixed RGB-distance tolerance.
                    base=covariance(i)+covariance(j)
                    cov=np.broadcast_to(base,(len(ids),3,3)).copy()
                    temporal=sigma[v,u]**2+sigma[vv[ids],uu[ids]]**2
                    cov+=np.eye(3)[None,:,:]*temporal[:,None,:]
                    for grad in ((source[25]-source[23])*.5,(source[31]-source[17])*.5):
                        cov+=np.outer(grad,grad)[None,:,:]/12.
                    for grad in ((samples[:,25]-samples[:,23])*.5,(samples[:,31]-samples[:,17])*.5):
                        cov+=grad[:,:,None]*grad[:,None,:]/12.
                    diff=samples[:,24,:]-source[24]
                    color_score[ids]=np.einsum('ij,ijk,ik->i',diff,np.linalg.inv(cov),diff)
                scores.append(corr);center_errors.append(err);center_scores.append(color_score);valids.append(ok)
            joint=np.minimum(scores[0],scores[1]);eligible=valids[0]&valids[1]&(np.maximum(center_scores[0],center_scores[1])<=12.838)
            joint[~eligible]=-1.;best=int(np.argmax(joint))
            if joint[best]<.9:
                reject('three_view_appearance_not_confirmed')
                if len(failed_matches)<16:
                    failed_matches.append(dict(view=i,pixel=[u,v],candidates=len(xyz),best_joint=float(joint[best]),
                        ncc_max=[float(s.max()) for s in scores],center_min=[float(e.min()) for e in center_errors]))
                continue
            ray_depth=(xyz[:,0]-half)*si+(xyz[:,1]-half)*co
            distance=np.abs(ray_depth-ray_depth[best])*voxel_mm
            resolution=mm/min(abs(np.sin(np.deg2rad(delta[j]))) for j in neighbors)
            far=distance>2*resolution
            if far.any() and joint[best]-joint[far].max()<.05:reject('depth_not_distinctive');continue
            near=joint>=joint[best]-.02
            width=float(np.ptp(ray_depth[near])*voxel_mm)
            if width>2*resolution:reject('depth_interval_too_wide');continue
            # Both neighbors must independently prefer the same depth interval.
            if any(abs(ray_depth[int(np.argmax(s))]-ray_depth[best])*voxel_mm>resolution for s in scores):
                reject('neighbor_depth_disagreement');continue
            reverse_verified=True
            for j in neighbors:
                uf,vf,_,_=project(xyz[best:best+1],j)
                target=interpolated_patches(frames[j],vf,uf,params[j]['lut'])[0]
                shifts=np.arange(-8,9);legal=(u+shifts>=4)&(u+shifts<796);shifts=shifts[legal]
                alternatives=patches(frames[i],np.full(len(shifts),v),u+shifts,params[i]['lut'])
                reverse=ncc(target[None,:,:],alternatives)
                best_shift=int(shifts[int(np.argmax(reverse))])
                origin=float(reverse[shifts==0][0]);farther=np.abs(shifts)>2
                if abs(best_shift)>1 or (farther.any() and origin-float(reverse[farther].max())<.03):
                    reverse_verified=False;break
            if not reverse_verified:reject('bidirectional_match_not_unique');continue
            # Joint quantization: a nearest-grid rounding can contradict a
            # silhouette although the continuous photo point does not. Search
            # only its eight bracketing centers; retain all other-view checks,
            # the observed pixel footprint, depth uncertainty and attachment.
            floor=np.floor(xyz[best]).astype(int)
            offsets=np.array([(dx,dy,dz) for dx in (0,1) for dy in (0,1) for dz in (0,1)])
            choices=floor+offsets
            choices=choices[np.argsort(np.sum((choices-xyz[best])**2,axis=1))]
            chosen=None
            for candidate in choices:
                vx,vy,vz=map(int,candidate)
                if not (0<=vx<n and 0<=vy<n and 0<=vz<n) or src[vz,vy,vx]:continue
                delta_depth=abs(((vx-xyz[best,0])*si+(vy-xyz[best,1])*co)*voxel_mm)
                if delta_depth>resolution:continue
                pu,pv,_,_=project(np.asarray([candidate],float),i)
                if abs(float(pu[0])-u)>.5*voxel_mm/mm*(abs(co)+abs(si))+1e-6:continue
                if abs(float(pv[0])-v)>.5*voxel_mm/mm+1e-6:continue
                ju=int((int(ofu[i])+vx*int(kx[i])+vy*int(ky[i]))>>16)
                jv=int((int(ofv)-vz*int(kv))>>16)
                if (jv,ju) not in interior_set:continue
                consistent=True
                for j in range(len(angles)):
                    if j==i:continue
                    ju=int((int(ofu[j])+vx*int(kx[j])+vy*int(ky[j]))>>16)
                    jv=int((int(ofv)-vz*int(kv))>>16)
                    if not (0<=ju<800 and 0<=jv<480 and sil[j,jv,ju]):consistent=False;break
                if not consistent:continue
                adjacent=any(src[vz+dz,vy+dy,vx+dx] for dx,dy,dz in ((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)) if 0<=vx+dx<n and 0<=vy+dy<n and 0<=vz+dz<n)
                if adjacent:chosen=(vx,vy,vz);break
            if chosen is None:reject('no_consistent_attached_voxel_for_matched_point');continue
            vx,vy,vz=chosen
            additions[vz,vy,vx]=True
            evidence.append(dict(view=i,pixel_uv=[u,v],voxel_xyz=[vx,vy,vz],neighbor_views=neighbors,
                background_score=float(bg['candidate_scores'][point_index]),background_threshold=bg['threshold'],
                ncc=[float(s[best]) for s in scores],center_RGB_RMS=[float(e[best]) for e in center_errors],
                center_normalized_color_score=[float(s[best]) for s in center_scores],
                quantization_error_mm=float(np.linalg.norm(np.asarray(chosen)-xyz[best])*voxel_mm),
                depth_interval_width_mm=width,pixel_parallax_resolution_mm=float(resolution),
                continuous_point_xyz_mm=[float((xyz[best,0]-half)*voxel_mm),float((xyz[best,1]-half)*voxel_mm),float(xyz[best,2]*voxel_mm)]))
    additions &= ~src
    return additions,dict(version=VERSION,internal_zero_pixels=candidate_count,
        background_rejected_pixels=background_rejected,accepted_evidence=evidence,
        added_surface_voxels=int(additions.sum()),rejections=rejections,failed_match_examples=failed_matches,
        masks_modified=False,PL_volume_modified=False,whole_rays_filled=False,
        camera_depth_sign=int(camera_depth_sign),
        new_depth_is_photo_hypothesis_not_physical_accuracy_certificate=True,
        elapsed_host_or_board_s=time.monotonic()-started)
