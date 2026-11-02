"""Low-contrast evidence, Python 3.6 / NumPy, with every dense score in PL.

Sparse colour prototypes are fitted from measured object and empty-scene pixels.
PL compares those prototypes, the spatial reference and source neighbours.
PS selects binary regions; unverified material is a separate UNDECIDED mask.
No image warp, global threshold lowering, geometric fill or object-class prior.
"""
import numpy as np
import time
import hashlib
VERSION=76

def _patches(frame,reference,lut,coords,driver):
    c=np.asarray(coords,int);y,x=c.T;dy,dx=np.mgrid[-2:3,-2:3]
    yy=y[:,None]+dy.ravel();xx=x[:,None]+dx.ravel()
    cur=driver._rgb(frame[yy,xx]);ref=driver._rgb(reference[yy,xx]).astype(float)
    cur=np.stack([lut[k,cur[...,k]] for k in range(3)],-1).astype(float)
    phase=(dy.ravel()+dx.ravel())%2==0
    means=np.stack([(cur-ref)[:,phase].mean(1),(cur-ref)[:,~phase].mean(1)],1)
    return means,ref.mean(1),cur,ref

def _null_models(frame,reference,lut,window,driver,context=None):
    left,_,right,bottom=window
    y,x=np.mgrid[8:max(9,bottom-4):16,8:792:16]
    take=(x<left-4)|(x>=right+4)
    coords=np.stack([y[take],x[take]],1)
    yy=np.arange(8,bottom-5,8);xx=np.arange(left+8,right-5,8)
    rim=np.r_[np.stack([yy,np.full(len(yy),left+4)],1),np.stack([yy,np.full(len(yy),right-5)],1),np.stack([np.full(len(xx),4),xx],1)]
    coords=np.r_[coords[np.linspace(0,len(coords)-1,min(128,len(coords))).astype(int)] if len(coords) else np.empty((0,2),int),rim]
    unfiltered_count=len(coords)
    if context is not None:
        a,b,c,d=context;yy=np.arange(max(4,b),min(bottom-4,d),8);xx=np.arange(max(left+4,a),min(right-4,c),8)
        border=np.r_[np.stack([yy,np.full(len(yy),max(left+3,a-4))],1),np.stack([yy,np.full(len(yy),min(right-4,c+3))],1),np.stack([np.full(len(xx),max(3,b-4)),xx],1)]
        # Context controls are admitted only when the original reference's
        # texture is observed; equal flat colour alone cannot certify them.
        import interior_evidence
        texture,_,_=interior_evidence._patch_evidence(frame,reference,lut,None,border,driver)
        m,_,_,_=_patches(frame,reference,lut,border,driver)
        close=np.all(np.max(abs(m),axis=1)<=np.array([8.,4.,8.])*1.5,axis=1)
        coords=np.r_[coords,border[texture|close]]
    mean,ref,_,_=_patches(frame,reference,lut,coords,driver)
    # Spatially disjoint calibration groups must both explain background noise.
    group=((coords[:,0]//32+coords[:,1]//32)%2);models=[]
    if len(coords)<32 or min((group==0).sum(),(group==1).sum())<12:
        return None,dict(accepted=False,reason='insufficient_independent_background_controls',samples=len(coords))
    cached={}
    for fold in (0,1):
        models.append([])
        for brightness in range(16):
            same=(ref.mean(1).astype(int)>>4)==brightness
            conditional=bool(((group==fold)&same).sum()>=12 and ((group!=fold)&same).sum()>=8)
            key=(fold,brightness if conditional else None)
            if key in cached:
                models[-1].append(cached[key]);continue
            chosen=(group==fold)&same if conditional else group==fold
            a=mean[chosen].reshape(-1,3);center=np.median(a,0)
            scale=np.maximum(np.median(abs(a-center),0)*1.4826,np.array([8.,4.,8.])/np.sqrt(12.))
            clipped=np.clip(a-center,-3*scale,3*scale)
            covariance=np.cov(clipped,rowvar=False)+np.diag(np.array([8.,4.,8.])**2/12.)
            inverse=np.linalg.inv(covariance)
            control=mean[(group!=fold)&same] if conditional else mean[group!=fold]
            delta=control-center;loss=np.einsum('nfc,cd,nfd->nf',delta,inverse,delta)
            threshold=max(12.838,float(np.percentile(np.min(loss,axis=1),99.5)))
            cached[key]=(center,inverse,threshold)
            models[-1].append(cached[key])
    return models,dict(samples=len(coords),spatial_calibration_folds=2,
        _control_coords=coords,_control_mean=mean,_control_reference_mean=ref,
        _control_group=group,_unfiltered_count=unfiltered_count)

def search_domain(anchors):
    """Horizontal and vertical measured brackets, never a silhouette or fill.

    Every accepted pixel still requires PL colour/reference and graph evidence.
    Real holes inside these brackets still compete with the empty background.
    """
    result=np.zeros_like(anchors)
    for y,row in enumerate(anchors):
        xs=np.flatnonzero(row)
        if len(xs)>1:result[y,xs[0]:xs[-1]+1]=True
    for x,col in enumerate(anchors.T):
        ys=np.flatnonzero(col)
        if len(ys)>1:result[ys[0]:ys[-1]+1,x]=True
    return result

def fit(values,maximum=4):
    a=np.asarray(values,float)
    if len(a)<16:return []
    centers=[np.median(a,axis=0)]
    for _ in range(1,min(maximum,len(a)//16)):
        dist=np.min(np.sum((a[:,None]-np.asarray(centers)[None])**2,axis=2),axis=1)
        j=int(np.argmax(dist))
        if dist[j]<64:break
        centers.append(a[j])
    centers=np.asarray(centers);label=np.zeros(len(a),int)
    for _ in range(10):
        label=np.argmin(np.sum((a[:,None]-centers[None])**2,axis=2),axis=1)
        for j in range(len(centers)):
            if (label==j).any():centers[j]=a[label==j].mean(0)
    models=[]
    floor=np.array([8.,4.,8.])**2/6.
    for j in range(len(centers)):
        hit=a[label==j]
        if len(hit)<8:continue
        variance=np.maximum(np.mean((hit-centers[j])**2,axis=0),floor)
        models.append(dict(center=centers[j].tolist(),variance=variance.tolist(),weight=len(hit)/len(a)))
    return models

def spatial_parameters(reference,noise,cfg,sl,driver):
    ref_sample=driver._rgb(reference[sl][::4,::4]);variance=np.tile((np.array([8.,4.,8.])**2/6.)[:,None],(1,16))
    if noise is not None:
        measured=np.asarray(noise)[sl][::4,::4]**2
        for channel in range(3):
            for k in range(16):
                hit=(ref_sample[...,channel]>>4)==k
                if hit.any():variance[channel,k]+=float(np.percentile(measured[...,channel][hit],90))
    inv=np.clip(np.rint(65536./variance),1,65535).astype(np.uint16)
    return dict(lut=cfg['lut'],inverse_variance=inv,low=int(np.ceil(2.366*65536)),high=int(np.ceil(7.815*65536)))

def sparse_model_cost(mapped,models,driver):
    best=np.full(len(mapped),np.inf);support=np.zeros(len(mapped),bool)
    for model in models:
        center=driver._rgb(np.uint16(model['reference565'])).astype(np.int64)
        inv=np.clip(np.rint(65536./np.asarray(model['variance'])),1,65535).astype(np.int64)
        score=np.sum((mapped.astype(np.int64)-center)**2*inv,axis=1)
        distance=np.where(score>model['high_q16'],24.,np.where(score>model['low_q16'],8.,2.))
        cost=distance*.5+.5*np.log(model['variance']).sum()-np.log(model['weight'])
        best=np.minimum(best,cost);support |= score<=model['high_q16']
    return best,support

def regional_gate(frame,reference,cfg,labels,review,foreground,background,spatial_possible,window,context,driver,regional_calibration=False):
    left,_,right,bottom=window;a,b,c,d=context;models,calibration=_null_models(frame,reference,cfg['lut'],window,driver,context=(left+a,b,left+c,d))
    if models is None:
        return np.zeros_like(review),np.zeros_like(review),dict(calibration=calibration,regions_reviewed=0)
    coords=calibration.pop('_control_coords');control_mean=calibration.pop('_control_mean');ref_mean=calibration.pop('_control_reference_mean');group=calibration.pop('_control_group');ncontrol=calibration.pop('_unfiltered_count')
    # Only physically declared outside/rim controls count towards acceptance;
    # appearance-selected context controls may train but cannot validate a fit.
    raw=driver._rgb(frame[coords[:,0],coords[:,1]]);mapped=np.stack([cfg['lut'][k,raw[:,k]] for k in range(3)],1)
    fc,fs=sparse_model_cost(mapped,foreground,driver);bc,_=sparse_model_cost(mapped,background,driver)
    null_losses=[]
    for i in range(ncontrol):
        model=models[1-int(group[i])][min(15,int(ref_mean[i].mean())>>4)];center,inverse,_=model
        delta=control_mean[i]-center;null_losses.append(float(np.einsum('fc,cd,fd->f',delta,inverse,delta).min()))
    joint=fs[:ncontrol]&spatial_possible[coords[:ncontrol,0],coords[:ncontrol,1]]&(np.asarray(null_losses)>7.815)
    fraction=float(joint.mean()) if len(joint) else 1.
    calibration.update(heldout_joint_false_candidate_fraction=fraction,maximum_joint_fraction=.01,physical_control_count=ncontrol,accepted=bool(ncontrol>=64 and fraction<=.01),
        reference_colour_bins=[dict(bin=k,controls=int(((ref_mean[:ncontrol].mean(1).astype(int)>>4)==k).sum()),false_candidates=int(joint[(ref_mean[:ncontrol].mean(1).astype(int)>>4)==k].sum())) for k in range(16)])
    strong=np.zeros(int(labels.max())+1,bool);weak=strong.copy();details=[]
    indices=np.flatnonzero(review);order=np.argsort(labels.ravel()[indices],kind='mergesort');indices=indices[order]
    unique,starts,counts=np.unique(labels.ravel()[indices],return_index=True,return_counts=True)
    eligible=np.argsort(-counts,kind='mergesort')[:1024]
    sample_indices=[];sample_offsets=[0];sample_cache={}
    for index in eligible:
        count=int(counts[index])
        if count not in sample_cache:sample_cache[count]=np.linspace(0,count-1,min(16,count)).astype(int)
        selected=indices[starts[index]+sample_cache[count]]
        sample_indices.append(selected);sample_offsets.append(sample_offsets[-1]+len(selected))
    samples=np.concatenate(sample_indices) if sample_indices else np.empty(0,int)
    yy,xx=np.divmod(samples,labels.shape[1]);yy=yy+b;xx=xx+left+a
    raw=driver._rgb(frame[yy,xx]);all_ref=driver._rgb(reference[yy,xx]).astype(float)
    all_cur=np.stack([cfg['lut'][k,raw[:,k]] for k in range(3)],1).astype(float)
    for position,index in enumerate(eligible):
        label=int(unique[index]);count=int(counts[index]);take=sample_cache[count]
        ref=all_ref[sample_offsets[position]:sample_offsets[position+1]]
        cur=all_cur[sample_offsets[position]:sample_offsets[position+1]]
        residual=cur-ref;means=np.stack([residual[::2].mean(0),residual[1::2].mean(0) if len(residual)>1 else residual[0]]);brightness=min(15,int(ref.mean())>>4)
        score=float('inf');agreement=1.;empirical_support=True
        for model in models:
            center,inverse,threshold=model[brightness];delta=means-center;loss=np.einsum('fc,cd,fd->f',delta,inverse,delta)
            score=min(score,float(loss.min()));agreement=min(agreement,float(delta[0]@inverse@delta[1]/max(np.sqrt(loss[0]*loss[1]),1e-12)))
            empirical_support &= bool(loss.min()>threshold)
        # The independent per-colour null thresholds already measure the
        # background explanation on two spatial folds. A few unrelated false
        # candidates in the full-frame control pool do not invalidate a region
        # that passes BOTH of those empirical thresholds. Keep the original
        # route unchanged unless this bounded regional review is requested.
        admissible=(ncontrol>=64) if regional_calibration else calibration['accepted']
        if len(take)>=4 and admissible and empirical_support and score>12.838 and agreement>.8:strong[label]=True
        if score>7.815:weak[label]=True
        details.append(dict(label=int(label),pixels=count,samples=len(take),minimum_fold_score=score,fold_agreement=agreement,independent_empirical_thresholds_passed=bool(empirical_support),foreground_accepted=bool(strong[label])))
    return strong[labels],weak[labels],dict(calibration=calibration,regional_calibration=bool(regional_calibration),reason=('independent_regional_null_thresholds_on_both_spatial_folds' if regional_calibration else 'regional_and_palette_evidence_jointly_validated' if calibration['accepted'] else 'background_control_failed_only_undecided_changes_retained'),regions_reviewed=len(details),regions_over_budget=int(max(0,len(unique)-len(eligible))),evidence=details)

def recover(device,frame,reference,cfg,ground,noise,body,strong,possible,driver,progress=None,sink=None,axis_col=None):
    import subject_region as sub,edge_partition as edge,interior_evidence as interior
    started=time.monotonic();left,_,right,_=map(int,getattr(driver,'measurement_roi',(160,0,640,480)));bottom=int(ground)
    measured=interior._erode(strong&body,1)[:bottom,left:right]
    yy0,xx0=np.nonzero(measured)
    scope=None
    if len(yy0):
        a0,b0,c0,d0=int(xx0.min()),int(yy0.min()),int(xx0.max())+1,int(yy0.max())+1
        mx,my=32+.1*(c0-a0),32+.1*(d0-b0)
        scope=[max(0,int(a0-mx)),max(0,int(b0-my)),min(right-left,int(np.ceil(c0+mx))),min(bottom,int(np.ceil(d0+my)))]
    info=dict(version=VERSION,applied=False,added_pixels=0,removed_pixels=0,method='sparse_prototypes_PL_likelihood_binary_graph',geometry_fill=False,PL_passes=0)
    empty=np.zeros_like(body)
    if scope is None:return body,empty,empty.copy(),info
    a,b,c,d=scope;sl=(slice(b,d),slice(left+a,left+c));old=body[sl];h,w=old.shape
    anchors=interior._erode(strong&body,1)[sl];supported=search_domain(anchors)
    horizontal=np.zeros_like(anchors)
    for y,row in enumerate(anchors):
        xs=np.flatnonzero(row)
        if len(xs)>1:horizontal[y,xs[0]:xs[-1]+1]=True
    candidate=supported&~old
    if not candidate.any():info['reason']='no_bracketed_negative_region';return body,empty,empty.copy(),info
    def classify(ref,parameters,kind):
        parameters=dict(parameters,info=dict(configuration_kind=kind,low_contrast_recovery_version=66))
        record=dict(reference_sha256=hashlib.sha256(np.ascontiguousarray(ref).tobytes()).hexdigest(),parameters=parameters,kind=kind)
        if kind.startswith(('prototype_foreground_','prototype_background_')):record['constant_reference565']=int(ref[0,0])
        if sink is not None:
            sink['records'].append(record);sink.update(parameters=parameters,modeled_reference=ref,strong=None,possible=None,status=None,packed=None)
        S,P,status,packed=device.classify(frame,ref,parameters)
        record.update(status=status,packed=packed);info['PL_passes']+=1
        if sink is not None:sink.update(strong=S,possible=P,status=status,packed=packed)
        return S,P
    if progress:progress.update('PL核对低对比候选')
    _,spatial_possible=classify(reference,spatial_parameters(reference,noise,cfg,sl,driver),'prototype_spatial_reference')
    if not (candidate&spatial_possible[sl]).any():
        info['reason']='all_gap_pixels_agree_with_empty_reference';return body,empty,empty.copy(),info
    core=interior._erode(strong&body,1);ys,xs=np.nonzero(core)
    if len(ys)<32:return body,empty,empty.copy(),info
    take=np.linspace(0,len(ys)-1,min(1024,len(ys))).astype(int)
    raw=driver._rgb(frame[ys[take],xs[take]]);fg=np.stack([cfg['lut'][k,raw[:,k]] for k in range(3)],1)
    yy,xx=np.mgrid[b:d:4,left+a:left+c:4];bg=driver._rgb(reference[yy,xx]).reshape(-1,3)
    if len(bg)>4096:bg=bg[np.linspace(0,len(bg)-1,4096).astype(int)]
    foreground=fit(np.concatenate([fg*gain for gain in np.linspace(.5,1.,9)]));background=fit(bg)
    if not foreground or not background:return body,empty,empty.copy(),info
    costs=[];class_support=[];identity=np.tile(np.arange(256,dtype=np.uint8),(3,1))
    constant=np.empty_like(frame)
    for name,models in (('foreground',foreground),('background',background)):
        cost=np.full((h,w),np.inf);supported_by_model=np.zeros((h,w),bool)
        for i,model in enumerate(models):
            center=np.rint(np.clip(model['center'],0,255)*np.array([31.,63.,31.])/255.).astype(np.uint16)
            packed=(center[0]<<11)|(center[1]<<5)|center[2];constant.fill(packed)
            inv=np.clip(np.rint(65536./np.asarray(model['variance'])),1,65535).astype(np.uint16)
            params=dict(lut=cfg['lut'],inverse_variance=np.repeat(inv[:,None],16,axis=1),low=int(4*65536),high=int(np.ceil(12.838*65536)))
            if progress:progress.update('PL前景/背景颜色竞争',name+' %d/%d'%(i+1,len(models)))
            S,P=classify(constant,params,'prototype_'+name+'_'+str(i))
            supported_by_model |= ~S[sl]
            distance=np.where(S[sl],24.,np.where(P[sl],8.,2.))
            unary=distance*.5+.5*np.log(model['variance']).sum()-np.log(model['weight'])
            cost=np.minimum(cost,unary)
            model.update(reference565=int(packed),low_q16=params['low'],high_q16=params['high'])
        costs.append(cost);class_support.append(supported_by_model)
    gradients=[]
    # A spatial match to the actual empty image competes with the global
    # palette, so a purple background cannot win merely by matching the object.
    for axis in (1,0):
        other=np.empty_like(frame)
        if axis==1:other[:,:-1]=frame[:,1:];other[:,-1]=frame[:,-1]
        else:other[:-1]=frame[1:];other[-1]=frame[-1]
        params=dict(lut=identity,inverse_variance=np.tile(np.array([1024,4096,1024],np.uint16)[:,None],(1,16)),
            low=int(np.ceil((81./64*2+25./16)*65536)),high=int(np.ceil(12.838*65536)))
        S,P=classify(other,params,'prototype_edge_'+str(axis));gradients.append((S[sl],P[sl]))
    yy,xx=np.indices((h,w));tile=(yy//8)*((w+7)//8)+xx//8
    try:labels=edge.regions(tile,gradients[0][1],gradients[1][1])
    except ValueError as error:
        if str(error)!='source edge complexity exceeds bounded alternate algorithm':raise
        info.update(reason='source_edge_budget_exceeded_no_recovery_claimed',needs_review=True)
        return body,empty,empty.copy(),info
    n=int(labels.max())+1
    if n>16384:info.update(reason='region_budget_exceeded');return body,empty,empty.copy(),info
    costs[0]+=np.where(spatial_possible[sl],0.,8.)
    margin=costs[1]-costs[0]
    regional_strong,regional_weak,region_info=regional_gate(frame,reference,cfg,labels,
        supported&class_support[0]&spatial_possible[sl]&~old,
        foreground,background,spatial_possible,(left,0,right,bottom),scope,driver)
    graph_performed=bool(regional_strong.any())
    if graph_performed:
        baseline=np.minimum(costs[0],costs[1]);fcost=costs[0]-baseline;bcost=costs[1]-baseline
        fgcost=np.bincount(labels.ravel(),weights=fcost.ravel(),minlength=n)
        bgcost=np.bincount(labels.ravel(),weights=bcost.ravel(),minlength=n)
        seed=np.bincount(labels.ravel(),weights=core[sl].ravel(),minlength=n)>0
        boundary=np.zeros((h,w),bool);boundary[0]=True;boundary[-1]=True;boundary[:,0]=True;boundary[:,-1]=True
        bgseed=np.bincount(labels.ravel(),weights=(boundary&~old).ravel(),minlength=n)>0
        capacity=float(fgcost.sum()+bgcost.sum()+h*w*16+1);fgcost[bgseed]=capacity;bgcost[seed&~bgseed]=capacity
        graph=edge.Flow(n+2)
        for j in range(n):graph.add(n,j,bgcost[j]);graph.add(j,n+1,fgcost[j])
        pairs=[];weights=[]
        for axis,(S,P) in enumerate(gradients):
            if axis==0:la,lb=labels[:,:-1],labels[:,1:];gs,gp=S[:,:-1],P[:,:-1]
            else:la,lb=labels[:-1],labels[1:];gs,gp=S[:-1],P[:-1]
            hit=la!=lb;lo,hi=np.minimum(la[hit],lb[hit]),np.maximum(la[hit],lb[hit])
            pairs.append(lo*n+hi);weights.append(np.where(gs[hit],.005,np.where(gp[hit],.1,2.)))
        keys,index=np.unique(np.concatenate(pairs),return_inverse=True);energy=np.bincount(index,weights=np.concatenate(weights))
        for k,weight in zip(keys,energy):
            u,v=int(k//n),int(k%n);graph.add(u,v,weight);graph.add(v,u,weight)
        reachable=graph.solve(n,n+1);keep=np.array([j in reachable for j in range(n)])[labels]
    else:
        keep=np.zeros_like(old)
    proposal=body.copy();proposal[sl]|=keep
    for item in sub.components(proposal,body,driver):
        if item['strong']==0:
            for y,x0,x1 in item['runs']:proposal[y,x0:x1]=False
    # This recovery only revisits gaps bracketed by measured main-object
    # evidence; colour alone cannot extend an unreliable exterior contour.
    margin=costs[1]-costs[0]
    additions=np.zeros_like(body)
    additions[sl]=(proposal[sl]&~old)&supported&class_support[0]&spatial_possible[sl]&(margin>np.log(3.))
    uncertain=np.zeros_like(body)
    # A background palette can also resemble an occluding material. Its
    # relative score cannot prove that material absent: retain overlapping
    # explanations as UNDECIDED, subject to independent regional checks.
    uncertain[sl]=supported&class_support[0]&spatial_possible[sl]&~old&~additions[sl]
    if sink is not None and sink.get('capture_candidate_debug'):
        sink['candidate_debug']=dict(slice_xyxy=(left+a,b,left+c,d),labels=labels,supported=supported,spatial=spatial_possible[sl],foreground_support=class_support[0],margin=margin,graph=keep,proposed_additions=additions[sl].copy(),proposed_unknown=uncertain[sl].copy(),regional_strong=regional_strong,regional_weak=regional_weak)
    # A lone positive at a colour/quantization boundary is insufficient for
    # new definite material. Thin or marginal proposals remain UNDECIDED;
    # measured original thin features are never eroded or removed.
    new_additions=interior._erode(additions[sl]&regional_strong,1)
    uncertain[sl]=((uncertain[sl]|additions[sl])&regional_weak&(horizontal|regional_strong))&~new_additions
    additions[sl]=new_additions
    # Verify large ambiguous regions against original reference texture. A
    # preserved textured background is a veto, not an object-colour cue.
    review=additions[sl]|uncertain[sl];coords=[];owners=[]
    for label in np.unique(labels[review]):
        ys0,xs0=np.nonzero((labels==label)&review)
        if len(ys0)<4:continue
        for k in np.linspace(0,len(ys0)-1,min(2,len(ys0))).astype(int):
            y,x=int(ys0[k]+b),int(xs0[k]+left+a)
            if 3<=y<477 and 3<=x<797:coords.append((y,x));owners.append(int(label))
        if len(coords)>=128:break
    texture_veto=0
    if coords:
        same,_,_=interior._patch_evidence(frame,reference,cfg['lut'],noise,coords,driver);owners=np.asarray(owners)
        for label in np.unique(owners):
            hit=owners==label
            if hit.sum()>=2 and same[hit].all():
                veto=labels==label;texture_veto+=int((review&veto).sum());additions[sl][veto]=False;uncertain[sl][veto]=False
    proposal=body|additions
    info.update(applied=bool(additions.any() or uncertain.any()),added_pixels=int(additions.sum()),models_foreground=foreground,models_background=background,
        foreground_training_pixels=len(fg),background_training_pixels=len(bg),class_priors=[.5,.5],regions=n,context_local_xyxy=scope,
        source_region_graph_performed=graph_performed,texture_veto_pixels=texture_veto,texture_patch_centers=len(coords),regional_validation=region_info,
        scope_of_recovery='colour_and_edge_supported_internal_gaps_only_no_exterior_extension',
        uncertain_pixels=int(uncertain.sum()),score_margin_log3=float(np.log(3.)),
        uncertainty_includes_colour_and_region_disagreement=True,not_calibrated_material_probability=True,
        uncertainty_palette_overlap_has_no_likelihood_margin_cutoff=True,
        elapsed_s=time.monotonic()-started)
    return proposal,additions,uncertain,info
