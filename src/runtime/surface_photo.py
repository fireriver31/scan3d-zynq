"""Orthographic photo attributes on actual exposed voxel faces (v32).

This module changes attributes, never occupancy. A box-filtered density gradient
estimates macroscopic direction solely for choosing a photo; triangles remain
unchanged. Visibility is rasterized from the actual side quads, not a sparse
point cloud. Each exposed face owns its color independently.
"""
import numpy as np

NUMPY_COMPAT_FIX_VERSION = 41
PERFORMANCE_VERSION = 64

DIRECTIONS = ((2,1),(2,-1),(1,1),(1,-1),(0,1),(0,-1))


def exposed_faces(occ):
    src=np.asarray(occ,dtype=bool)
    for axis,sign in DIRECTIONS:
        mask=src.copy()
        a=[slice(None)]*3;b=a.copy()
        a[axis]=slice(0,-1) if sign>0 else slice(1,None)
        b[axis]=slice(1,None) if sign>0 else slice(0,-1)
        mask[tuple(a)] &= ~src[tuple(b)]
        yield np.nonzero(mask)


def _blur_xy(a):
    """Five-tap binomial estimate, air outside; bounded plane-sized storage."""
    for axis in (0,1):
        pad=[(0,0),(0,0)];pad[axis]=(2,2)
        padded=np.pad(a,pad,mode='constant');out=np.zeros_like(a)
        for j,w in enumerate((1.,4.,6.,4.,1.)):
            s=[slice(None)]*2;s[axis]=slice(j,j+a.shape[axis])
            out += padded[tuple(s)]*(w/16.)
        a=out
    return a


def surface_directions(occ, surface_zyx):
    """Density normals at surface centers; no full float 3-D intermediate."""
    z,y,x=surface_zyx;n=occ.shape[0];normals=np.zeros((len(z),3),np.float32)
    cache={}
    def plane(iz):
        if iz not in cache:
            a=np.zeros(occ.shape[1:],np.float32)
            for dz,w in zip(range(-2,3),(1.,4.,6.,4.,1.)):
                if 0<=iz+dz<n:a+=occ[iz+dz]*(w/16.)
            cache[iz]=_blur_xy(a)
        return cache[iz]
    for iz in np.unique(z):
        ids=np.flatnonzero(z==iz);yy=y[ids];xx=x[ids]
        mid=plane(int(iz));pad=np.pad(mid,1,mode='constant')
        normals[ids,0]=pad[yy+1,xx]-pad[yy+1,xx+2]
        normals[ids,1]=pad[yy,xx+1]-pad[yy+2,xx+1]
        normals[ids,2]=plane(int(iz)-1)[yy,xx]-plane(int(iz)+1)[yy,xx]
        for old in list(cache):
            if old<int(iz):del cache[old]
    length=np.linalg.norm(normals,axis=1)
    normals/=np.maximum(length[:,None],1e-12)
    return normals


def nearest_face_samples(ids,pixels,distance,face_count):
    """Exactly the stable lexsort(distance, face-id) first sample, without sort."""
    best=np.full(face_count,np.inf)
    np.minimum.at(best,ids,distance)
    candidates=np.flatnonzero(distance==best[ids])
    first=np.full(face_count,len(ids),np.intp)
    np.minimum.at(first,ids[candidates],candidates)
    unique=np.flatnonzero(first<len(ids))
    return unique,pixels[first[unique]]


def colorize_faces(occ, voxel_mm, frames565, masks, angles_deg, calibration,
                   channel_gains=None, camera_depth_sign=1):
    """Pick one observed photograph per exposed face, never average materials.

    Side quads rasterize their projected area at original image pixel centers.
    Only the nearest intersection at a pixel is eligible. Subpixel faces with
    no covered camera pixel and horizontal faces stay explicitly unobserved.
    This remains the calibrated orthographic approximation, not recovered depth.
    """
    src=np.asarray(occ,dtype=bool);frames=np.asarray(frames565)
    silhouettes=np.asarray(masks,dtype=bool);angles=np.asarray(angles_deg,float)
    if src.ndim!=3 or len(set(src.shape))!=1 or not src.any():
        raise ValueError('面取色需要非空立方占据')
    if frames.dtype!=np.uint16 or frames.ndim!=3 or frames.shape!=silhouettes.shape or len(frames)!=len(angles):
        raise ValueError('面取色的照片、掩膜和角度不一致')
    if not np.isfinite(angles).all() or not np.isfinite(voxel_mm) or voxel_mm<=0:
        raise ValueError('面取色角度或采样间距无效')
    if camera_depth_sign not in (-1,1):
        raise ValueError('相机深度方向必须为 -1 或 1')
    scale=float(voxel_mm)/float(calibration['mm_per_px'])
    axis=float(calibration['axis_col']);ground=float(calibration['v_ground'])
    if not np.isfinite([scale,axis,ground]).all() or scale<=0:
        raise ValueError('面取色标定无效')
    h,w=frames.shape[1:];n=src.shape[0];half=(n-1)/2.
    # Surface indexing follows scan_pipeline._surface_mask: z,y,x order.
    groups=list(exposed_faces(src))
    surface=np.zeros_like(src)
    for z,y,x in groups:surface[z,y,x]=True
    sz,sy,sx=np.nonzero(surface);flat=np.flatnonzero(surface)
    normal=surface_directions(src,(sz,sy,sx))
    records=[]
    for (ax,sign),(z,y,x) in zip(DIRECTIONS,groups):
        ids=np.searchsorted(flat,np.ravel_multi_index((z,y,x),src.shape))
        nn=normal[ids].copy();component=2-ax
        # A thin wall has distinct opposing physical faces. A center gradient
        # cannot describe both, so only use it when consistent with this face.
        consistent=nn[:,component]*sign>1e-5
        nn[~consistent]=0;nn[~consistent,component]=sign
        records.append(dict(z=z,y=y,x=x,surface_ids=ids,normal=nn,
            rgb=np.full((len(z),3),180,np.uint8),valid=np.zeros(len(z),bool),
            best=np.full(len(z),-np.inf,np.float32),view=np.full(len(z),-1,np.int16),
            previous_rgb=np.zeros((len(z),3),np.uint8),previous_seen=np.zeros(len(z),bool)))
    agreement=[]
    for i,angle in enumerate(angles):
        co,si=np.cos(np.deg2rad(angle)),np.sin(np.deg2rad(angle))
        camera=np.array([-si,-co,0.])*camera_depth_sign
        depth_buffer=np.full(h*w,-np.inf,np.float32);samples=[]
        for direction,rec in enumerate(records[:4]):
            ax,sign=DIRECTIONS[direction]
            if camera[2-ax]*sign<=1e-8:
                samples.append(None);continue
            z,y,x=rec['z'],rec['y'],rec['x']
            fx=x.astype(float)-half;fy=y.astype(float)-half
            if ax==2:
                fx+=sign*.5;du=-si*.5*scale;dd=-co*.5*voxel_mm
            else:
                fy+=sign*.5;du=co*.5*scale;dd=-si*.5*voxel_mm
            uc=axis+(fx*co-fy*si)*scale
            dc=-(fx*si+fy*co)*voxel_mm
            dc*=camera_depth_sign;dd*=camera_depth_sign
            vc=ground-z*scale
            umin=uc-abs(du);umax=uc+abs(du)
            vmin=vc-.5*scale;vmax=vc+.5*scale
            u0=np.ceil(umin-1e-8).astype(np.intp)
            v0=np.ceil(vmin-1e-8).astype(np.intp)
            all_ids=[];all_pixels=[];all_depths=[]
            # Individual quads project to <= scale pixels per side. Process
            # bounded vector batches; no triangle-by-pixel Python iteration.
            for dv in range(int(np.ceil(scale))+1):
                vv=v0+dv
                for dx in range(int(np.ceil(2*abs(du)))+1):
                    uu=u0+dx
                    ok=(uu>=0)&(uu<w)&(vv>=0)&(vv<h)&(uu<=umax+1e-8)&(vv<=vmax+1e-8)
                    ids=np.flatnonzero(ok)
                    ids=ids[silhouettes[i,vv[ids],uu[ids]]]
                    if not len(ids):continue
                    dep=dc[ids]+(uu[ids]-uc[ids])*(dd/du)
                    pixels=vv[ids]*w+uu[ids]
                    np.maximum.at(depth_buffer,pixels,dep.astype(np.float32))
                    all_ids.append(ids);all_pixels.append(pixels);all_depths.append(dep)
            samples.append(None if not all_ids else (np.concatenate(all_ids),np.concatenate(all_pixels),np.concatenate(all_depths),uc,vc))
        pair_errors=[]
        for rec,sample in zip(records[:4],samples):
            if sample is None:
                rec['previous_seen'].fill(False)
                continue
            ids,pixels,dep,uc,vc=sample
            visible=dep>=depth_buffer[pixels]-max(1e-6,voxel_mm*1e-5)
            ids=ids[visible];pixels=pixels[visible]
            if not len(ids):
                rec['previous_seen'].fill(False)
                continue
            # If a face covers multiple pixels choose the one nearest its
            # center, not an arithmetic blend across an image/material edge.
            distance=(pixels%w-uc[ids])**2+(pixels//w-vc[ids])**2
            # Choose the covered, visible camera pixel closest to this face.
            ids,pixels=nearest_face_samples(ids,pixels,distance,len(rec['z']))
            score=rec['normal'][ids].dot(camera)
            packed=frames[i].ravel()[pixels]
            rr=(packed>>11)&31;gg=(packed>>5)&63;bb=packed&31
            sample_rgb=np.stack(((rr<<3)|(rr>>2),(gg<<2)|(gg>>4),(bb<<3)|(bb>>2)),axis=1).astype(np.uint8)
            was_seen=rec['previous_seen'][ids]
            if was_seen.any():
                error=np.mean(np.abs(sample_rgb[was_seen].astype(float)-rec['previous_rgb'][ids[was_seen]].astype(float)),axis=1)
                pair_errors.append(error)
            rec['previous_seen'].fill(False);rec['previous_seen'][ids]=True;rec['previous_rgb'][ids]=sample_rgb
            eligible=(score>0)&(score>rec['best'][ids]+1e-7)
            ids=ids[eligible];sample_rgb=sample_rgb[eligible];score=score[eligible]
            rec['rgb'][ids]=sample_rgb
            rec['best'][ids]=score;rec['view'][ids]=i;rec['valid'][ids]=True
        errors=np.concatenate(pair_errors) if pair_errors else np.array([])
        gap=0. if i==0 else abs((float(angle)-float(angles[i-1])+180.)%360.-180.)
        if not 0.<gap<=45.:errors=np.array([])
        agreement.append(dict(view=int(i),pairs=int(len(errors)),median_error=None if not len(errors) else float(np.median(errors)),
            clipped_mean_error=None if not len(errors) else float(np.minimum(errors,64).mean())))
    counts=np.zeros(len(angles),np.int64)
    for rec in records:
        if channel_gains is not None:
            ok=rec['valid'];rec['rgb'][ok]=np.clip(np.rint(rec['rgb'][ok].astype(float)*channel_gains),0,255).astype(np.uint8)
        counts+=np.bincount(rec['view'][rec['valid']],minlength=len(angles))
    # The point cloud is a separate diagnostic: representative observed face.
    point_rgb=np.full((len(flat),3),180,np.uint8);point_best=np.full(len(flat),-np.inf)
    for rec in records:
        ids=rec['surface_ids'];ok=rec['valid']&(rec['best']>point_best[ids])
        point_rgb[ids[ok]]=rec['rgb'][ok];point_best[ids[ok]]=rec['best'][ok]
    diag=dict(implementation_version=32,normal_method='local_density_gradient_for_photo_selection_only',
        geometry_modified=False,visibility='rasterized_exposed_side_quads_nearest_depth',
        independent_face_colors=True,backfacing_photo_copies=0,
        observed_faces=int(sum(d['valid'].sum() for d in records)),
        total_exposed_faces=int(sum(len(d['z']) for d in records)),
        photo_selected_faces_per_view=counts.tolist(),unobserved_faces_stay_gray=True,
        horizontal_faces_unobserved=True,physical_accuracy_verified=False)
    diag.update(camera_depth_sign=camera_depth_sign,adjacent_photo_agreement=agreement)
    return dict(face_colors=[r['rgb'] for r in records],face_valid=[r['valid'] for r in records],
        face_views=[r['view'] for r in records],point_colors=point_rgb,
        point_valid=np.isfinite(point_best),diagnostics=diag)


def choose_depth_direction(positive_diagnostics, negative_diagnostics):
    """Select with alternating adjacent pairs, then validate on the other half.

    Weak/no texture or inconclusive evidence keeps the previous coordinate
    convention with an explicit inconclusive flag. Neither mask fit nor model
    volume can disambiguate this sign in an orthographic silhouette model.
    """
    a=positive_diagnostics['adjacent_photo_agreement']
    b=negative_diagnostics['adjacent_photo_agreement']
    pairs=[(i,x['clipped_mean_error'],y['clipped_mean_error']) for i,(x,y) in enumerate(zip(a,b))
           if min(x['pairs'],y['pairs'])>=256 and x['clipped_mean_error'] is not None and y['clipped_mean_error'] is not None]
    result=dict(selected_sign=1,confirmed=False,method='alternating_adjacent_pair_photo_validation',
                minimum_pairs_per_fold=4,relative_improvement_required=.15,
                absolute_RGB_error_improvement_required=2.,view_win_fraction_required=.75,
                usable_view_pairs=len(pairs),reason='insufficient_consistent_photo_evidence')
    folds=[]
    for parity in (0,1):
        values=np.asarray([(x,y) for i,x,y in pairs if i%2==parity],float)
        if len(values)<4:return result
        means=values.mean(axis=0);win=int(np.argmin(means));lose=1-win
        improvement=float(means[lose]-means[win]);relative=improvement/max(float(means[lose]),1e-9)
        wins=float(np.mean(values[:,win]<values[:,lose]))
        folds.append(dict(parity=parity,pairs=len(values),positive_error=float(means[0]),negative_error=float(means[1]),
            preferred_sign=1 if win==0 else -1,relative_improvement=relative,absolute_improvement=improvement,
            win_fraction=wins,passed=relative>=.15 and improvement>=2. and wins>=.75))
    result['folds']=folds
    if all(f['passed'] for f in folds) and folds[0]['preferred_sign']==folds[1]['preferred_sign']:
        result.update(selected_sign=folds[0]['preferred_sign'],confirmed=True,
                      reason='both_disjoint_pair_sets_prefer_same_depth_direction')
    return result


def colorize_faces_auto(occ,voxel_mm,frames565,masks,angles_deg,calibration,channel_gains=None):
    positive=colorize_faces(occ,voxel_mm,frames565,masks,angles_deg,calibration,channel_gains,1)
    if len(angles_deg)<9:
        positive['diagnostics']['depth_direction_selection']=dict(selected_sign=1,confirmed=False,reason='too_few_views')
        return positive
    negative=colorize_faces(occ,voxel_mm,frames565,masks,angles_deg,calibration,channel_gains,-1)
    selection=choose_depth_direction(positive['diagnostics'],negative['diagnostics'])
    chosen=negative if selection['selected_sign']==-1 else positive
    chosen['diagnostics']['depth_direction_selection']=selection
    return chosen
