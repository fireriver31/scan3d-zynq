"""Review missing low-contrast regions using already completed PL evidence.

No full-image RGB scoring, threshold change, image warp, vote relaxation or
geometric filling. The optional segmentation route separates accepted region
foreground from unresolved support; its input arrays remain immutable.
"""
from pathlib import Path
import hashlib
import json
import types
import time
import numpy as np
VERSION=80
PERFORMANCE_VERSION=81

def _batch_source_statistics(frame,reference,lut,noise,eligible,area,order,starts,width,left,driver):
    count=len(eligible);groups={};offsets={};samples=[None]*count
    for i,label in enumerate(eligible):
        size=int(area[label]);length=min(16,size)
        if size not in offsets:offsets[size]=np.linspace(0,size-1,length).astype(int)
        ids=order[starts[label]+offsets[size]];samples[i]=ids
        groups.setdefault(length,[]).append(i)
    scores=np.zeros(count);agreement=np.zeros(count);gains=np.zeros((count,2));shadow=np.zeros(count);shadow_agreement=np.zeros(count)
    fallback_count=0
    for length,positions in groups.items():
        positions=np.asarray(positions,int);ids=np.stack([samples[i] for i in positions]);y,x=np.divmod(ids,width);x=x+left
        raw=driver._rgb(frame[y,x]);ref=driver._rgb(reference[y,x]);cur=np.stack([lut[c,raw[...,c]] for c in range(3)],-1)
        residual=cur.astype(float)-ref;folds=(np.arange(length)%2)==0
        means=np.stack((residual[:,folds].mean(1),residual[:,~folds].mean(1)),1)
        variance=np.broadcast_to(np.array([8.,4.,8.])**2,(len(positions),3)).copy()
        if noise is not None:variance+=np.median(np.asarray(noise)[y,x]**2,axis=1)
        loss=np.sum(means**2/variance[:,None,:],axis=2)
        direction=np.sum(means[:,0]*means[:,1]/variance,axis=1)/np.maximum(np.sqrt(loss[:,0]*loss[:,1]),1e-12)
        losses=[];alpha=[];chromatic=[]
        for train in (folds,~folds):
            test=~train;r0=np.ascontiguousarray(ref[:,train].astype(float));c0=np.ascontiguousarray(cur[:,train].astype(float))
            numerator=np.ascontiguousarray(r0*c0/variance[:,None,:]).reshape(len(positions),-1).sum(1)
            denominator=np.ascontiguousarray(r0*r0/variance[:,None,:]).reshape(len(positions),-1).sum(1)
            gain=np.minimum(1.,np.maximum(0.,numerator/np.maximum(denominator,1e-9)))
            error=(cur[:,test].astype(float)-gain[:,None,None]*ref[:,test]).mean(1)
            losses.append(np.sum(error**2/variance,axis=1));alpha.append(gain);chromatic.append(error)
        chromatic=np.stack(chromatic,1);minloss=np.minimum(losses[0],losses[1])
        consistency=np.sum(chromatic[:,0]*chromatic[:,1]/variance,axis=1)/np.maximum(np.sqrt(losses[0]*losses[1]),1e-12)
        minimum=np.minimum(loss[:,0],loss[:,1]);alpha=np.stack(alpha,1)
        # Preserve the scalar path for values arbitrarily close to a decision
        # threshold; the batching must not become a segmentation change.
        near=(abs(minimum-9.)<1e-9)|(abs(direction-.8)<1e-9)|(abs(minloss-9.)<1e-9)|(abs(consistency-.8)<1e-9)
        for j in np.flatnonzero(near):
            a=means[j];v=variance[j];ls=np.sum(a*a/v,axis=1)
            direction[j]=float(np.sum(a[0]*a[1]/v)/max(np.sqrt(ls[0]*ls[1]),1e-12));minimum[j]=float(ls.min())
            ca=[];sl=[];al=[]
            for train in (folds,~folds):
                r=ref[j,train].astype(float);c=cur[j,train].astype(float)
                gain=float(np.sum(r*c/v)/max(float(np.sum(r*r/v)),1e-9));gain=min(1.,max(0.,gain))
                e=(cur[j,~train].astype(float)-gain*ref[j,~train]).mean(0);ca.append(e);sl.append(float(np.sum(e*e/v)));al.append(gain)
            consistency[j]=float(np.sum(ca[0]*ca[1]/v)/max(np.sqrt(sl[0]*sl[1]),1e-12));minloss[j]=min(sl);alpha[j]=al;fallback_count+=1
        scores[positions]=minimum;agreement[positions]=direction;gains[positions]=alpha;shadow[positions]=minloss;shadow_agreement[positions]=consistency
    good=(scores>9.)&(agreement>.8)&(shadow>9.)&(shadow_agreement>.8)
    return dict(samples=samples,scores=scores,agreement=agreement,gains=gains,shadow=shadow,
                shadow_agreement=shadow_agreement,good=good,threshold_boundary_scalar_fallbacks=fallback_count)


def _review_source_supported_regions(frame,reference,parameters,noise,body,confirmed,records,driver):
    import edge_partition as edge,subject_region as sub,interior_evidence as interior
    material_started=time.monotonic()
    by_kind={r['kind']:r for r in records if 'packed' in r}
    required=('prototype_spatial_reference','prototype_edge_1','prototype_edge_0')
    empty=np.zeros_like(body,bool)
    info=dict(version=80,performed=False,additional_PL_passes=0,source='completed_PL_spatial_strong_and_source_edges',regions=[])
    if not all(k in by_kind for k in required):return empty,dict(info,reason='completed_PL_evidence_unavailable')
    spatial,possible=driver.unpack_decisions(by_kind[required[0]]['packed'])
    left,_,right,bottom=map(int,driver.measurement_roi);sl=(slice(0,bottom),slice(left,right));h,w=body[sl].shape
    g=[driver.unpack_decisions(by_kind[k]['packed'])[1][sl] for k in required[1:]]
    yy,xx=np.indices((h,w));tile=(yy//8)*((w+7)//8)+xx//8
    try:labels=edge.regions(tile,*g)
    except ValueError as e:return empty,dict(info,reason=str(e))
    n=int(labels.max())+1
    if n>16384:return empty,dict(info,reason='region_budget_exceeded')
    candidate=(spatial&~body&~confirmed)[sl]
    flat=labels.ravel();order=np.argsort(flat,kind='mergesort');area=np.bincount(flat,minlength=n);ends=np.cumsum(area);starts=ends-area
    counts=np.bincount(labels[candidate],minlength=n)
    eligible=np.flatnonzero((counts>0)&(area>=4));eligible=eligible[np.argsort(-counts[eligible],kind='mergesort')[:2048]]
    accepted=np.zeros(n,bool);details=[];patch_coords=[];owners=[]
    statistics_started=time.monotonic()
    statistics=_batch_source_statistics(frame,reference,parameters['lut'],noise,eligible,area,order,starts,w,left,driver)
    statistics_s=time.monotonic()-statistics_started
    for index,k in enumerate(eligible):
        ids=statistics['samples'][index];y,x=np.divmod(ids,w);x=x+left
        good=bool(statistics['good'][index])
        if good:
            coords=np.array([(int(y[j]),int(x[j])) for j in np.linspace(0,len(y)-1,min(2,len(y))).astype(int)])
            legal=[(int(y0),int(x0)) for y0,x0 in coords if 4<=y0<476 and 4<=x0<796]
            if len(legal)>=2 and len(patch_coords)+len(legal)<=512:
                patch_coords.extend(legal);owners.extend([int(k)]*len(legal));accepted[k]=True
        details.append(dict(label=int(k),pixels=int(area[k]),candidate_pixels=int(counts[k]),
            minimum_independent_fold_score=float(statistics['scores'][index]),fold_agreement=float(statistics['agreement'][index]),
            shadow_attenuation_folds=statistics['gains'][index].tolist(),minimum_shadow_residual_score=float(statistics['shadow'][index]),
            shadow_residual_agreement=float(statistics['shadow_agreement'][index]),source_change_accepted=good))
    if patch_coords:
        matched,_,_=interior._patch_evidence(frame,reference,parameters['lut'],noise,patch_coords,driver)
        owners=np.asarray(owners)
        for k in np.unique(owners):
            hit=owners==k
            if hit.sum()>=2 and matched[hit].all():accepted[k]=False
    added=np.zeros_like(body,bool);added[sl]=candidate&accepted[labels]
    # Edge oversegmentation can turn a measured high-contrast feature into
    # one-pixel labels. Retain those already PL-strong pixels when they connect
    # to verified material. This never sets an unmeasured neighbour positive.
    tiny_added=0
    if 'original_reference_colour' in by_kind:
        original,_=driver.unpack_decisions(by_kind['original_reference_colour']['packed'])
        tiny=(spatial&original&~confirmed&~body)[sl]&(area[labels]<4)
        connected=(body|added)[sl].copy();before=connected.copy()
        for _ in range(64):
            near=np.zeros_like(connected);near[1:]|=connected[:-1];near[:-1]|=connected[1:];near[:,1:]|=connected[:,:-1];near[:,:-1]|=connected[:,1:]
            take=tiny&near&~connected
            if not take.any():break
            connected|=take
        tiny_added=int((connected&~before).sum());added[sl]|=connected&~body[sl]
    # A changed region outside this one observed object is not an extra object.
    combined=body|added;selected=np.zeros_like(body,bool)
    for item in sub.components(combined,body,driver):
        if item['strong']:
            for y,a,b in item['runs']:selected[y,a:b]=True
    added &= selected
    info.update(performed=True,reason='two_independent_source_folds_actual_PL_and_observed_body_connection',regions=details,
        region_count=n,regions_reviewed=len(eligible),regions_over_budget=max(0,int(((counts>0)&(area>=4)).sum())-len(eligible)),
        patch_centers=len(patch_coords),added_foreground_pixels=int(added.sum()),original_foreground_removed=0,
        maximum_patch_centers=512,dense_RGB_classification_on_PS=False,whole_region_or_hole_fill=False,
        retained_small_labels_with_original_and_spatial_PL_strong=tiny_added,
        shadow_compatibility_only_blocks_new_promotions=True,unresolved_shadow_like_material_may_remain_unclassified=True)
    info.update(performance_implementation_version=81,elapsed_s=time.monotonic()-material_started,
        batched_statistics_s=statistics_s,threshold_boundary_scalar_fallbacks=statistics['threshold_boundary_scalar_fallbacks'])
    return added,info


def retain_measured_parts(body,strong,possible,driver,axis_col=None,frame=None,
                          reference=None,cfg=None,noise=None,maximum_patch_centers=64):
    """Review lost P-components only after the original background route.

    A seven-pixel erosion locates the principal body; it cannot certify that
    a thin, separated, PL-supported nearby part is background. No zero PL
    pixel, negative silhouette ray, or unmeasured connection is made positive.
    """
    import interior_evidence as interior
    from subject_region import components
    left,_,right,bottom=map(int,driver.measurement_roi)
    mask=body[:bottom,left:right];s=strong[:bottom,left:right];p=possible[:bottom,left:right]
    added=np.zeros_like(body,bool);markers=np.zeros_like(body,bool)
    info=dict(version=75,retained_components=0,retained_possible_pixels=0,
        retained_strong_pixels=0,background_texture_veto_pixels=0,patch_centers=0,
        original_foreground_removed=0,negative_PL_pixels_added=0,
        phase='after_original_background_route_and_speckle_cleanup',components=[],
        physical_material_certified=False)
    anchors=components(mask&s,mask&s,driver)
    if not anchors:return added,markers,info
    axis=(right-left)*.5 if axis_col is None else float(axis_col)-left
    def score(v):
        a,b,c,d=v['box'];return v['strong']/(1.+4.*((.5*(a+c)-axis)/mask.shape[1])**2)
    main=max(anchors,key=score);a,b,c,d=main['box'];margin=max(12.,.2*max(c-a,d-b))
    eligible=[]
    for item in components(p,s,driver):
        if item['strong']<6 or any(mask[y,l:r].any() for y,l,r in item['runs']):continue
        x0,y0,x1,y1=item['box'];dx,dy=max(a-x1,x0-c,0),max(b-y1,y0-d,0)
        if x0==0 or y0==0 or x1==mask.shape[1] or dx*dx+dy*dy>margin*margin:continue
        eligible.append(item)
    eligible.sort(key=lambda v:-v['strong'])
    coords=[];owners=[]
    for i,item in enumerate(eligible):
        remaining=maximum_patch_centers-len(coords)
        if remaining<=0:break
        for y,x in interior._centers(item,min(2,remaining)):
            if 3<=y<477 and 3<=x+left<797:coords.append((y,x+left));owners.append(i)
    same=np.zeros(len(coords),bool)
    if coords and frame is not None and reference is not None and cfg is not None:
        same,_,_=interior._patch_evidence(frame,reference,cfg['lut'],noise,coords,driver)
    owners=np.asarray(owners,int)
    for i,item in enumerate(eligible):
        hit=owners==i
        veto=bool(hit.sum()>=2 and same[hit].all())
        record={k:v for k,v in item.items() if k!='runs'}
        record.update(background_texture_veto=veto,patch_centers=int(hit.sum()),retained=not veto)
        info['components'].append(record)
        if veto:info['background_texture_veto_pixels']+=item['area'];continue
        for y,l,r in item['runs']:
            added[y,l+left:r+left]=p[y,l:r]
            markers[y,l+left:r+left]=s[y,l:r]
        info['retained_components']+=1
    info.update(retained_possible_pixels=int(added.sum()),retained_strong_pixels=int(markers.sum()),
        patch_centers=len(coords),main_anchor_local_xyxy=main['box'],part_margin_px=margin)
    return added,markers,info


def review(frame,reference,parameters,noise,ground,body,upper,confirmed,
           baseline,records,recovery,driver,axis_col=None,source_reference=None,foreground_output=False):
    import subject_region as sub,competitive_colour as colour,edge_partition as edge
    import interior_evidence as interior
    left,_,right,_=map(int,driver.measurement_roi);bottom=int(np.floor(ground))
    original_strong,original_possible=driver.unpack_decisions(baseline)
    baseline_parameters=next((r['parameters'] for r in records if r.get('kind')=='original_reference_colour'),parameters)
    parts,_,part_info=retain_measured_parts(body,original_strong,original_possible,
        driver,axis_col,frame,reference if source_reference is None else source_reference,baseline_parameters,noise)
    excluded=body if foreground_output else upper
    extra=parts&~excluded&~confirmed
    positive=extra.copy() if foreground_output else np.zeros_like(body)
    def output(extra,info):
        if not foreground_output:return extra,info
        local_positive,local_review=_review_source_supported_regions(
            frame,reference,parameters,noise,body|positive,confirmed,records,driver)
        positive[:] |= local_positive
        extra=extra|local_positive
        info['source_region_material_review']=local_review
        strong=positive&extra&~confirmed&~body
        unknown=extra&~strong
        info.update(binary_foreground_review_version=80,
            accepted_foreground_pixels=int(strong.sum()),
            remaining_unknown_pixels=int(unknown.sum()),
            all_unknown_promoted=False,source_strict_changed=bool(strong.any()),
            original_strict_changed=bool(strong.any()))
        return strong,unknown,info
    info=dict(version=VERSION,additional_PL_passes=0,original_strict_changed=False,
        added_uncertainty_pixels=0,known_background_veto=True,parts=part_info,
        measured_material_certified=False,regional_review_performed=False,
        source_upper_sha256=hashlib.sha256(np.asarray(upper,np.uint8).tobytes()).hexdigest(),
        source='actual_original_PL_and_completed_prototype_and_edge_passes')
    by_kind={r['kind']:r for r in records if 'packed' in r}
    required=('prototype_spatial_reference','prototype_edge_1','prototype_edge_0')
    palette=recovery.get('models_foreground',[])
    bg_palette=recovery.get('models_background',[])
    if not all(k in by_kind for k in required) or not palette or not bg_palette:
        info.update(reason='prototype_evidence_unavailable_only_measured_parts_reviewed',added_uncertainty_pixels=int(extra.sum()))
        return output(extra,info)
    _,spatial=driver.unpack_decisions(by_kind['prototype_spatial_reference']['packed'])
    foreground=np.zeros_like(body)
    for key,record in by_kind.items():
        if key.startswith('prototype_foreground_'):
            strong,_=driver.unpack_decisions(record['packed']);foreground |= ~strong
    # Palette training and all PL thresholds remain the original ones. Only
    # the review domain changes; disconnected measured parts are not eroded
    # out of existence before they can support a nearby low-contrast region.
    owned=body|parts;ys,xs=np.nonzero(owned)
    if not len(ys):return output(extra,dict(info,reason='no_owned_evidence'))
    old_scope=recovery.get('context_local_xyxy',[0,0,right-left,bottom])
    a,b,c,d=map(int,old_scope)
    a=min(a,max(0,int(xs.min())-left));c=max(c,min(right-left,int(xs.max())-left+1))
    b=min(b,int(ys.min()));d=max(d,min(bottom,int(ys.max())+1))
    sl=(slice(b,d),slice(left+a,left+c));h,w=owned[sl].shape
    anchors=(owned&original_strong)[sl]
    bracket=colour.search_domain(anchors)
    gradients=[driver.unpack_decisions(by_kind[k]['packed']) for k in ('prototype_edge_1','prototype_edge_0')]
    yy,xx=np.indices((h,w));tiles=(yy//8)*((w+7)//8)+xx//8
    try:labels=edge.regions(tiles,gradients[0][1][sl],gradients[1][1][sl])
    except ValueError as error:
        if str(error)!='source edge complexity exceeds bounded alternate algorithm':raise
        return output(extra,dict(info,reason='source_edge_budget_exceeded_only_measured_parts_reviewed',
            added_uncertainty_pixels=int(extra.sum())))
    n=int(labels.max())+1
    if n>16384:
        return output(extra,dict(info,reason='region_budget_exceeded',added_uncertainty_pixels=int(extra.sum())))
    seeds=bracket&spatial[sl]&foreground[sl]&~excluded[sl]&~confirmed[sl]
    supported_labels=np.bincount(labels[seeds],minlength=n)>0
    region_review=supported_labels[labels]&~body[sl]&~confirmed[sl]
    if not region_review.any():
        return output(extra,dict(info,reason='no_new_PL_supported_source_regions',added_uncertainty_pixels=int(extra.sum())))
    # Inspect whole source-edge regions, rather than selecting only pixels
    # that already passed a noise threshold and biasing their mean upward.
    regional_strong,regional_weak,audit=colour.regional_gate(frame,reference,parameters,labels,
        region_review,palette,bg_palette,spatial,(left,0,right,bottom),(a,b,c,d),driver,regional_calibration=foreground_output)
    candidate=region_review&regional_weak&spatial[sl]&foreground[sl]&~excluded[sl]
    coords=[];owners=[]
    candidate_ids=np.flatnonzero(candidate)
    candidate_ids=candidate_ids[np.argsort(labels.ravel()[candidate_ids],kind='mergesort')]
    candidate_labels,candidate_starts,candidate_counts=np.unique(labels.ravel()[candidate_ids],return_index=True,return_counts=True)
    for label,start,count in zip(candidate_labels,candidate_starts,candidate_counts):
        y,x=np.divmod(candidate_ids[start:start+count],labels.shape[1])
        if len(y)<4:continue
        for k in np.linspace(0,len(y)-1,min(2,len(y))).astype(int):
            yy0,xx0=int(y[k]+b),int(x[k]+left+a)
            if 3<=yy0<477 and 3<=xx0<797:coords.append((yy0,xx0));owners.append(int(label))
        if len(coords)>=128:break
    veto=0
    if coords:
        matched,_,_=interior._patch_evidence(frame,reference,parameters['lut'],noise,coords,driver)
        owners=np.asarray(owners)
        for label in np.unique(owners):
            take=owners==label
            if take.sum()>=2 and matched[take].all():
                blocked=(labels==label)&candidate;veto+=int(blocked.sum());candidate[blocked]=False
    extra[sl] |= candidate
    if foreground_output:positive[sl] |= candidate&regional_strong
    extra &= ~excluded&~confirmed
    info.update(reason='whole_source_region_change_review_after_measured_part_ownership',
        regional_review_performed=True,regions=n,scope_local_xyxy=[a,b,c,d],
        regional_validation=audit,texture_patch_centers=len(coords),texture_veto_pixels=veto,
        added_uncertainty_pixels=int(extra.sum()),negative_original_PL_requires_regional_and_palette_evidence=True)
    return output(extra,info)


def review_scan(scan,calibration,directory=None,progress=None):
    """Old scans reuse their actual PL records; new scans reuse stage 7 audit."""
    import pl_mask as pm,interior_evidence as interior
    body=np.asarray(scan['masks'],bool);upper=np.asarray(scan.get('conservative_surface_masks',body),bool)
    added=np.zeros_like(body)
    summary=dict(version=VERSION,performed=False,added_uncertainty_pixels=0,
        additional_PL_passes=0,strict_masks_changed=False,physical_material_certified=False,per_view=[])
    if scan.get('source')!='pl_color_photos' or int(scan.get('low_contrast_recovery_version',0))<66:
        return added,dict(summary,reason='original_PL_palette_evidence_unavailable')
    if int(scan.get('source_evidence_version',0))>=75:
        if 'source_evidence_unknown_masks' not in scan:
            raise ValueError('区域证据版本缺少对应待定掩膜')
        added=np.asarray(scan['source_evidence_unknown_masks'],bool)
        if added.shape!=upper.shape or (added&~np.asarray(scan['interior_unknown_masks'],bool)).any():
            raise ValueError('区域待定证据与原掩膜不匹配')
        expected=scan.get('source_evidence_base_upper_sha256')
        if expected is not None:
            base=upper&~added
            if len(expected)!=len(base):raise ValueError('区域证据指纹数量不匹配')
            for i in range(len(base)):
                if hashlib.sha256(base[i].astype(np.uint8).tobytes()).hexdigest()!=str(expected[i]):
                    raise ValueError('区域证据来源掩膜指纹不匹配')
        summary.update(performed=True,reused_segmentation_audit=True,
            reason='same_saved_stage7_uncertainty_reused_without_repeated_review',
            added_uncertainty_pixels=int(added.sum()),
            source_upper_sha256=hashlib.sha256((upper&~added).astype(np.uint8).tobytes()).hexdigest())
        return added,summary
    required=('color_frames565','empty_color_frame565','background_noise_sigma',
        'pl_actual_pass_packed','pl_actual_pass_frame_indices','pl_actual_pass_kind',
        'pl_actual_pass_luts','pl_actual_pass_inverse_variances','pl_actual_pass_low_q16','pl_actual_pass_high_q16',
        'pl_baseline_packed_decisions','pl_luts','pl_inverse_variances','pl_low_q16','pl_high_q16',
        'interior_confirmed_background_masks','segmentation_diagnostics_json')
    if not all(k in scan for k in required):return added,dict(summary,reason='complete_actual_PL_records_unavailable')
    frames=np.asarray(scan['color_frames565']);reference=np.asarray(scan['empty_color_frame565'])
    if body.ndim!=3 or body.shape[1:]!=(480,800) or frames.shape!=body.shape or frames.dtype!=np.uint16:
        raise ValueError('区域证据与照片尺寸不一致')
    for key,value in (('color_frames_sha256',frames),('empty_frame_sha256',reference)):
        if key in scan and str(scan[key])!=hashlib.sha256(value.astype('<u2',copy=False).tobytes()).hexdigest():
            raise ValueError('区域证据原图指纹不匹配')
    packed=np.asarray(scan['pl_actual_pass_packed']);indices=np.asarray(scan['pl_actual_pass_frame_indices'])
    kinds=np.asarray(scan['pl_actual_pass_kind']);n=len(packed)
    if (packed.dtype!=np.uint32 or packed.shape!=(n,pm.WORDS) or len(indices)!=n or len(kinds)!=n or
            (indices<0).any() or (indices>=len(body)).any()):
        raise ValueError('区域复核的实际PL记录格式不一致')
    diagnostics=json.loads(str(scan['segmentation_diagnostics_json']))
    driver=types.SimpleNamespace(**vars(pm));driver.measurement_roi=interior.measurement_window(calibration,calibration['v_ground'])
    modeled_ids=np.asarray(scan.get('roi_modeled_reference_frame_indices',[]),int)
    models=scan.get('roi_modeled_references565')
    for i in range(len(body)):
        if progress:progress.update('复用PL证据核验低对比部件','%d/%d'%(i+1,len(body)))
        records=[]
        for j in np.flatnonzero(indices==i):
            cfg=dict(lut=np.asarray(scan['pl_actual_pass_luts'])[j],inverse_variance=np.asarray(scan['pl_actual_pass_inverse_variances'])[j],
                low=int(np.asarray(scan['pl_actual_pass_low_q16'])[j]),high=int(np.asarray(scan['pl_actual_pass_high_q16'])[j]))
            records.append(dict(kind=str(kinds[j]),parameters=cfg,packed=packed[j]))
        current=reference
        if diagnostics[i].get('background_appearance_model',{}).get('applied') or diagnostics[i].get('background_registration',{}).get('accepted'):
            hit=np.flatnonzero(modeled_ids==i)
            if models is None or len(hit)!=1:
                summary['per_view'].append(dict(view=i+1,reason='actual_modeled_reference_unavailable_original_evidence_retained'))
                continue
            current=np.asarray(models)[hit[0]]
        cfg=dict(lut=np.asarray(scan['pl_luts'])[i],inverse_variance=np.asarray(scan['pl_inverse_variances'])[i],
            low=int(np.asarray(scan['pl_low_q16'])[i]),high=int(np.asarray(scan['pl_high_q16'])[i]))
        extra,info=review(frames[i],current,cfg,scan['background_noise_sigma'],calibration['v_ground'],body[i],upper[i],
            np.asarray(scan['interior_confirmed_background_masks'][i],bool),np.asarray(scan['pl_baseline_packed_decisions'])[i],
            records,diagnostics[i].get('low_contrast_recovery',{}),driver,calibration['axis_col'],source_reference=reference)
        added[i]=extra;info['view']=i+1;summary['per_view'].append(info)
    summary.update(performed=True,reused_segmentation_audit=False,
        reason='actual_PL_parts_and_source_region_change_retained_as_unknown',
        added_uncertainty_pixels=int(added.sum()),source_upper_sha256=hashlib.sha256(upper.astype(np.uint8).tobytes()).hexdigest(),
        added_unknown_sha256=hashlib.sha256(added.astype(np.uint8).tobytes()).hexdigest())
    if directory is not None:
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
        name='1024_source_evidence_v75.npz';target=directory/name;temporary=target.with_name(target.stem+'.tmp.npz')
        np.savez_compressed(str(temporary),added_unknown_masks=added);temporary.replace(target)
        summary['unknown_masks_file']=name
        target=directory/'1024_source_evidence_v75.json';temporary=target.with_suffix('.tmp')
        temporary.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8');temporary.replace(target)
    return added,summary


_MATERIAL_INT_COLUMNS=('label','pixels','candidate_pixels')
_MATERIAL_FLOAT_COLUMNS=('minimum_independent_fold_score','fold_agreement','shadow_attenuation_0','shadow_attenuation_1','minimum_shadow_residual_score','shadow_residual_agreement')

def compact_material_diagnostics(diagnostic,frame_index):
    material=diagnostic.get('source_evidence_review',{}).get('source_region_material_review',{})
    details=material.get('regions',[])
    integers=np.asarray([[r[k] for k in _MATERIAL_INT_COLUMNS] for r in details],np.int32).reshape(-1,3)
    floats=np.asarray([[r['minimum_independent_fold_score'],r['fold_agreement'],r['shadow_attenuation_folds'][0],r['shadow_attenuation_folds'][1],r['minimum_shadow_residual_score'],r['shadow_residual_agreement']] for r in details],np.float64).reshape(-1,6)
    flags=np.asarray([r['source_change_accepted'] for r in details],np.uint8)
    if material:
        material['regions']=[]
        material['region_details_table']=dict(version=81,frame_index=int(frame_index),rows=len(details),
            storage='same_scan_npz',offsets='source_material_region_offsets',integers='source_material_region_ints',
            floats='source_material_region_floats',flags='source_material_region_accept')
    return integers,floats,flags

def material_diagnostic_arrays(tables):
    counts=[len(v[0]) for v in tables]
    return dict(source_material_region_details_version=np.int32(81),
        source_material_region_offsets=np.r_[0,np.cumsum(counts)].astype(np.int32),
        source_material_region_ints=np.concatenate([v[0] for v in tables],axis=0) if tables else np.empty((0,3),np.int32),
        source_material_region_floats=np.concatenate([v[1] for v in tables],axis=0) if tables else np.empty((0,6),np.float64),
        source_material_region_accept=np.concatenate([v[2] for v in tables]) if tables else np.empty(0,np.uint8),
        source_material_region_columns_json=json.dumps(dict(integers=_MATERIAL_INT_COLUMNS,floats=_MATERIAL_FLOAT_COLUMNS,flag='source_change_accepted'),separators=(',',':')))

def material_region_details(scan,frame_index,diagnostics=None):
    if diagnostics is None:diagnostics=json.loads(str(scan['segmentation_diagnostics_json']))
    material=diagnostics[frame_index].get('source_evidence_review',{}).get('source_region_material_review',{})
    table=material.get('region_details_table')
    if table is None:return material.get('regions',[])
    slot=int(table['frame_index']);offsets=np.asarray(scan['source_material_region_offsets'])
    a,b=map(int,offsets[slot:slot+2]);ii=np.asarray(scan['source_material_region_ints'])[a:b];ff=np.asarray(scan['source_material_region_floats'])[a:b];flags=np.asarray(scan['source_material_region_accept'])[a:b]
    if b-a!=int(table['rows']):raise ValueError('局部核验数值表行数不匹配')
    return [dict(label=int(x[0]),pixels=int(x[1]),candidate_pixels=int(x[2]),minimum_independent_fold_score=float(y[0]),fold_agreement=float(y[1]),
        shadow_attenuation_folds=[float(y[2]),float(y[3])],minimum_shadow_residual_score=float(y[4]),shadow_residual_agreement=float(y[5]),source_change_accepted=bool(flag)) for x,y,flag in zip(ii,ff,flags)]

def expand_material_diagnostics(scan):
    diagnostics=json.loads(str(scan['segmentation_diagnostics_json']))
    for i,d in enumerate(diagnostics):
        material=d.get('source_evidence_review',{}).get('source_region_material_review',{})
        if 'region_details_table' in material:
            material['regions']=material_region_details(scan,i,diagnostics);material.pop('region_details_table')
    return diagnostics

def save_scan_archive(path,scan,progress=None):
    """Standard .npy entries, closed ZIP contexts, atomic replacement."""
    import os,zipfile
    try:
        from exact_scan_accel import fast_zip_entry
    except ImportError:
        fast_zip_entry=None
    path=Path(path);started=time.monotonic();last=[started];state=dict(version=81,status='writing',fields_total=len(scan),fields_started=0,
        uncompressed_array_bytes=0,maximum_array_bytes=0,current_field=None,board_or_host='current_execution')
    target=path.parent/'1025_segmentation_performance_v81.json'
    if int(scan.get('previous_completed_segmentation_reused',0)):
        state.update(previous_completed_segmentation_reused=True,new_PL_calls_this_resume=0,
            reused_original_body_compute_s=float(scan.get('segmentation_elapsed_s',float('nan'))))
    def checkpoint():
        temporary=target.with_name(target.name+'.part')
        try:
            temporary.write_text(json.dumps(state,ensure_ascii=False,separators=(',',':')),encoding='utf8');temporary.replace(target)
        except OSError as error:state['timing_checkpoint_error']=str(error)
    checkpoint()
    temporary=path.with_name(path.name+'.part')
    try:
        with temporary.open('wb') as stream,zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as archive:
          for name,value in scan.items():
            array=np.asanyarray(value);state['fields_started']+=1;state['current_field']=name
            state['uncompressed_array_bytes']+=int(array.nbytes);state['maximum_array_bytes']=max(state['maximum_array_bytes'],int(array.nbytes))
            if progress is not None:
                try:progress.update('保存掩膜及诊断数据','%d/%d %s'%(state['fields_started'],state['fields_total'],name))
                except Exception as error:state['display_error']=str(error)
            if time.monotonic()-last[0]>1.:
                state['archive_elapsed_s']=time.monotonic()-started;checkpoint();last[0]=time.monotonic()
            # Writable ZIP entries are supported by Python 3.6. NumPy writes
            # the same dtype/shape and data order without an object pickle.
            with archive.open(name+'.npy','w',force_zip64=True) as entry:
                if fast_zip_entry is not None:fast_zip_entry(entry)
                np.lib.format.write_array(entry,array,allow_pickle=False)
        os.replace(str(temporary),str(path))
    except BaseException:
        state.update(status='interrupted_or_failed',archive_elapsed_s=time.monotonic()-started)
        checkpoint();raise
    state.update(status='completed',archive_elapsed_s=time.monotonic()-started,archive_file_bytes=path.stat().st_size)
    if progress is not None and hasattr(progress,'started'):state['segmentation_entry_elapsed_s']=time.monotonic()-progress.started
    checkpoint()


def _binary_components8(mask):
    mask=np.asarray(mask,bool);parent=[];sizes=[];runs=[];previous=[]
    def root(k):
        while parent[k]!=k:parent[k]=parent[parent[k]];k=parent[k]
        return k
    for y,row in enumerate(mask):
        cuts=np.flatnonzero(np.diff(np.r_[False,row,False].astype(np.int8)))
        current=[]
        for a,b in zip(cuts[::2],cuts[1::2]):
            node=len(parent);parent.append(node);sizes.append(int(b-a));current.append((int(a),int(b),node));runs.append((y,int(a),int(b),node))
        i=j=0
        while i<len(current) and j<len(previous):
            a,b,u=current[i];c,d,v=previous[j]
            if max(a,c)<=min(b,d):
                ru,rv=root(u),root(v)
                if ru!=rv:parent[max(ru,rv)]=min(ru,rv)
            if b<=d:i+=1
            else:j+=1
        previous=current
    labels=np.asarray([root(i) for i in range(len(parent))],int)
    areas=np.bincount(labels,weights=np.asarray(sizes),minlength=len(parent)).astype(np.int64) if len(parent) else np.empty(0,np.int64)
    return runs,labels,areas

def _largest_component8(mask):
    runs,labels,areas=_binary_components8(mask);out=np.zeros_like(mask,bool)
    if not len(areas):return out,dict(input_components=0,retained_pixels=0,removed_pixels=0)
    owner=int(np.argmax(areas))
    for y,a,b,node in runs:
        if labels[node]==owner:out[y,a:b]=True
    return out,dict(input_components=int((areas>0).sum()),retained_pixels=int(areas[owner]),removed_pixels=int(np.asarray(mask,bool).sum()-areas[owner]))

def _components_touching8(mask,seed):
    runs,labels,areas=_binary_components8(mask);keep=set();out=np.zeros_like(mask,bool)
    for y,a,b,node in runs:
        if seed[y,a:b].any():keep.add(int(labels[node]))
    for y,a,b,node in runs:
        if int(labels[node]) in keep:out[y,a:b]=True
    return out

def apply_main_component_policy(result):
    started=time.monotonic();original=np.asarray(result['body'],bool);interior=result['interior'];original_upper=np.asarray(interior['conservative'],bool)
    main,counts=_largest_component8(original);removed=original&~main
    upper=original_upper.copy();upper[removed]=False;upper|=main
    upper=_components_touching8(upper,main)
    # Removed confirmed foreground must not return as conservative material.
    upper[removed]=False
    result['component_original_body_packed']=np.packbits(original.ravel())
    result['component_original_upper_packed']=np.packbits(original_upper.ravel())
    result['body']=main;result['strong']=result['strong']&main;result['unknown']=result['unknown']&main
    result['evidence_foreground'] &= main;result['evidence_unknown'] &= upper&~main
    interior['unknown'] &= upper&~main;interior['inferred'] &= upper&~main;interior['conservative']=upper
    d=interior['diagnostics'];d.update(unknown_pixels=int(interior['unknown'].sum()),inferred_foreground_pixels=int(interior['inferred'].sum()),strict_component_scope_applied=True)
    audit=result['diagnostics']['source_evidence_review'];audit['source_upper_sha256']=hashlib.sha256(np.asarray(upper&~result['evidence_unknown'],np.uint8).tobytes()).hexdigest()
    counts.update(version=81,mode='strict_largest_8_connected_component',user_authorized_detached_true_parts_may_be_removed=True,
        connectivity=8,hole_filling=False,erosion=False,foreground_pixels_added=0,
        conservative_removed_pixels=int((original_upper&~upper).sum()),conservative_support_restricted_to_main=True,
        original_foreground_packed_saved=True,retained_new_evidence_pixels=int(result['evidence_foreground'].sum()),elapsed_s=time.monotonic()-started)
    result['diagnostics']['single_component_filter']=counts
    result['diagnostics']['segmentation_elapsed_s']+=counts['elapsed_s']
    return result


def _iter_material_diagnostics(text):
    text=str(text);decoder=json.JSONDecoder();position=0
    while position<len(text) and text[position].isspace():position+=1
    if position>=len(text) or text[position]!='[':raise ValueError('分割诊断必须是列表')
    position+=1
    while True:
        while position<len(text) and text[position].isspace():position+=1
        if position>=len(text):raise ValueError('分割诊断未写完整')
        if text[position]==']':return
        value,position=decoder.raw_decode(text,position);yield value
        while position<len(text) and text[position].isspace():position+=1
        if position>=len(text):raise ValueError('分割诊断未写完整')
        if text[position]==',':position+=1;continue
        if text[position]==']':return
        raise ValueError('分割诊断分隔符错误')

def recover_completed_save(color_scan,reference,ground,noise,calibration):
    import sys,traceback,gc
    previous=getattr(sys,'last_traceback',None)
    if previous is None or calibration is None:return None
    parent=Path(__file__).resolve().parent;node=previous;completed=None;closing=None;save_frame=False
    while node is not None:
        frame=node.tb_frame;filename=Path(frame.f_code.co_filename)
        if filename.parent==parent:
            if frame.f_code.co_name in ('_atomic_npz','save_scan_archive') and filename.name in ('scan_pipeline.py','subject_evidence.py'):save_frame=True
            if frame.f_code.co_name=='segment_scan' and filename.name=='pl_mask.py':
                completed=frame.f_locals.get('result');closing=frame.f_locals.get('closing')
        node=node.tb_next
    if not save_frame or not isinstance(completed,dict) or closing is None:return None
    required=('masks','conservative_surface_masks','interior_unknown_masks','interior_inferred_masks','interior_confirmed_background_masks',
        'photo_strong_masks','photo_unknown_masks','source_evidence_unknown_masks','region_foreground_packed',
        'segmentation_diagnostics_json','measurement_calibration_json','color_frames_sha256','empty_frame_sha256','angles_deg','closing_color_frame565')
    if not all(k in completed for k in required):return None
    try:
        frames=np.asarray(color_scan['frames565']);shape=frames.shape
        if shape!=(len(color_scan['angles_deg']),480,800) or frames.dtype!=np.uint16:return None
        if np.shape(completed['masks'])!=shape or np.shape(closing)!=(480,800):return None
        digest=lambda a:hashlib.sha256(memoryview(np.ascontiguousarray(np.asarray(a,dtype='<u2'))).cast('B')).hexdigest()
        if digest(frames)!=str(completed['color_frames_sha256']) or digest(reference)!=str(completed['empty_frame_sha256']):return None
        if not np.array_equal(completed['angles_deg'],color_scan['angles_deg']):return None
        if not np.array_equal(completed['closing_color_frame565'],color_scan['closing_frame565']):return None
        if str(completed.get('frame_pixel_order',''))!=str(color_scan.get('frame_pixel_order','')):return None
        oldcal=json.loads(str(completed['measurement_calibration_json']))
        if any(float(oldcal[k])!=float(calibration[k]) for k in ('left_px','right_px','axis_col','v_ground','mm_per_px')) or float(ground)!=float(oldcal['v_ground']):return None
        if (noise is None)!=('background_noise_sigma' not in completed):return None
        if noise is not None and not np.array_equal(completed['background_noise_sigma'],np.asarray(noise,np.float32)):return None
        count=0
        for d in _iter_material_diagnostics(completed['segmentation_diagnostics_json']):
            if not isinstance(d,dict) or int(d.get('source_evidence_review',{}).get('binary_foreground_review_version',0))!=80:return None
            count+=1
        if count!=len(frames):return None
    except (KeyError,ValueError,TypeError,OverflowError):return None
    result=dict(completed);closing=np.asarray(closing,bool).copy()
    # Drop only obsolete function locals after taking the completed result.
    # Global colour_scan/calibration/Overlay variables remain in the kernel.
    traceback.clear_frames(previous);gc.collect()
    return result,closing

def normalize_completed_save(scan):
    result=dict(scan);count=len(result['masks']);already_strict=(int(result.get('single_component_filter_version',0))>=81 and str(result.get('single_component_filter_mode',''))=='strict_largest_8_connected_component')
    packed_details=int(result.get('source_material_region_details_version',0))>=81
    if not already_strict:
        changed=('masks','photo_strong_masks','photo_unknown_masks','source_evidence_unknown_masks','region_foreground_packed','conservative_surface_masks','interior_unknown_masks','interior_inferred_masks')
        for key in changed:result[key]=np.array(result[key],copy=True)
        original_body=np.empty((count,48000),np.uint8);original_upper=original_body.copy()
    diagnostics=[];tables=[];review_times=[]
    for i,d in enumerate(_iter_material_diagnostics(result['segmentation_diagnostics_json'])):
        if not already_strict:
            interior=dict(conservative=result['conservative_surface_masks'][i].astype(bool),unknown=result['interior_unknown_masks'][i].astype(bool),
                inferred=result['interior_inferred_masks'][i].astype(bool),diagnostics=d['interior_evidence'])
            item=dict(body=result['masks'][i].astype(bool),interior=interior,strong=result['photo_strong_masks'][i].astype(bool),unknown=result['photo_unknown_masks'][i].astype(bool),
                evidence_foreground=np.unpackbits(result['region_foreground_packed'][i]).reshape(480,800).astype(bool),
                evidence_unknown=result['source_evidence_unknown_masks'][i].astype(bool),diagnostics=d)
            item=apply_main_component_policy(item)
            for dest,source in (('masks','body'),('photo_strong_masks','strong'),('photo_unknown_masks','unknown'),('source_evidence_unknown_masks','evidence_unknown')):result[dest][i]=item[source]
            for dest,source in (('conservative_surface_masks','conservative'),('interior_unknown_masks','unknown'),('interior_inferred_masks','inferred')):result[dest][i]=interior[source]
            result['region_foreground_packed'][i]=np.packbits(item['evidence_foreground'].ravel())
            original_body[i]=item['component_original_body_packed'];original_upper[i]=item['component_original_upper_packed']
        if not packed_details:tables.append(compact_material_diagnostics(d,i))
        review_times.append(d.get('source_evidence_review',{}).get('source_region_material_review',{}).get('elapsed_s',float('nan')))
        diagnostics.append(d)
    if not packed_details:result.update(material_diagnostic_arrays(tables))
    if not already_strict:
        result.update(single_component_filter_version=np.int32(81),single_component_filter_connectivity=np.int32(8),
            single_component_filter_mode='strict_largest_8_connected_component',component_filter_original_masks_packed=original_body,
            component_filter_original_upper_packed=original_upper)
    result['source_evidence_base_upper_sha256']=np.asarray([d['source_evidence_review']['source_upper_sha256'] for d in diagnostics])
    result['segmentation_diagnostics_json']=json.dumps(diagnostics,ensure_ascii=False,separators=(',',':'))
    result['source_material_review_elapsed_s']=np.asarray(review_times,np.float64)
    result['previous_completed_segmentation_reused']=np.uint8(1)
    result['new_PL_calls_this_resume']=np.int32(0)
    return result
