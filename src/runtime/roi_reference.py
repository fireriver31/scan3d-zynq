"""Sparse, held-out background appearance model; never produces object masks.

Estimate a smooth illumination field from 1/16 RGB samples whose reference
texture predicts held-out observations better than a smooth surface. Interpolate
only this nuisance field, not silhouettes or geometry. Pixel decisions run in PL.
Python 3.6 / NumPy; no object classes, dimensions, filling, or outline dilation.
"""
import time
import numpy as np

IMPLEMENTATION_VERSION = 26
PERFORMANCE_VERSION = 52


def rgb565(value):
    a = np.asarray(value, dtype=np.uint16)
    r, g, b = (a >> 11) & 31, (a >> 5) & 63, a & 31
    return np.stack(((r << 3) | (r >> 2), (g << 2) | (g >> 4),
                     (b << 3) | (b >> 2)), axis=-1).astype(np.float64)


def pack_nearest(rgb):
    a = np.clip(rgb, 0, 255)
    r = np.rint(a[..., 0]*31./255.).astype(np.uint16)
    g = np.rint(a[..., 1]*63./255.).astype(np.uint16)
    b = np.rint(a[..., 2]*31./255.).astype(np.uint16)
    return (r << 11) | (g << 5) | b


def align_reference(frame, reference, parameters, ground_row):
    """Accept a bounded translation only if unseen background blocks agree."""
    yy,xx=np.mgrid[4:max(5,int(ground_row)-4):4,4:796:4]
    anchor=(xx<160)|(xx>=640)
    y,x=yy[anchor],xx[anchor]
    rgb=rgb565(frame[y,x]).astype(int)
    observed=np.stack([parameters['lut'][c,rgb[:,c]] for c in range(3)],axis=-1).astype(float)
    blocks=(y//32)*25+x//32
    train=((y//32+x//32)%2)==0;test=~train
    candidates=[]
    for dy in (-1,0,1):
        for dx in (-1,0,1):
            expected=rgb565(reference[y+dy,x+dx])
            error=observed-expected
            error-=np.median(error[train],axis=0)
            scale=np.maximum(np.median(error[train]**2,axis=0),[16.,4.,16.])
            losses=np.minimum(np.sum(error**2/scale,axis=1),30.)
            candidates.append((float(np.mean(losses[train])),dy,dx,losses))
    zero=next(v for v in candidates if v[1:3]==(0,0))
    chosen=min(candidates,key=lambda v:(v[0],abs(v[1])+abs(v[2])))
    diffs=[]
    if chosen[1:3]!=(0,0):
        for block in np.unique(blocks[test]):
            hit=test&(blocks==block)
            if hit.sum()>=8:diffs.append(float(np.mean(zero[3][hit]-chosen[3][hit])))
    accepted=False;win=0.;mean=0.;sem=0.
    if len(diffs)>=8:
        values=np.asarray(diffs);win=float(np.mean(values>0));mean=float(values.mean())
        sem=float(values.std(ddof=1)/np.sqrt(len(values)))
        accepted=bool(win>=.75 and mean>3*sem and mean>0)
    dy,dx=chosen[1:3] if accepted else (0,0)
    padded=np.pad(reference,1,mode='edge')
    aligned=padded[1+dy:481+dy,1+dx:801+dx].copy() if (dy,dx)!=(0,0) else reference
    return aligned,dict(accepted=accepted,offset_yx=[dy,dx],search_radius_px=1,
                        heldout_blocks=len(diffs),heldout_win_fraction=win,
                        heldout_improvement=mean,heldout_standard_error=sem,
                        geometry_accuracy_verified=False)


def _interpolate(a, shape, spacing, origin=0.):
    y = np.clip((np.arange(shape[0])-origin)/spacing, 0, a.shape[0]-1)
    x = np.clip((np.arange(shape[1])-origin)/spacing, 0, a.shape[1]-1)
    iy = np.minimum(y.astype(int), a.shape[0]-2)
    ix = np.minimum(x.astype(int), a.shape[1]-2)
    wy, wx = (y-iy)[:, None, None], (x-ix)[None, :, None]
    return ((a[iy[:, None], ix]*(1-wx)+a[iy[:, None], ix+1]*wx)*(1-wy)+
            (a[iy[:, None]+1, ix]*(1-wx)+a[iy[:, None]+1, ix+1]*wx)*wy)


def fit_reference(frame, reference, parameters, ground_row):
    started = time.monotonic()
    sampled = rgb565(frame[::4, ::4])
    ref = rgb565(reference[::4, ::4])
    current = np.stack([parameters['lut'][c, sampled[..., c].astype(int)]
                        for c in range(3)], axis=-1).astype(np.float64)
    variance = 65536./parameters['inverse_variance'].astype(np.float64)
    noise = np.stack([variance[c, ref[..., c].astype(int) >> 4]
                      for c in range(3)], axis=-1)
    # The match compares two decoded RGB565 samples; half a native code is a
    # conservative local uncertainty floor. A single uniform quantizer's
    # q/sqrt(12) error is too optimistic for verifying a re-rendered reference.
    noise=np.maximum(noise,np.array([16.,4.,16.]))
    yy, xx = np.mgrid[:120, :200]
    bottom = int(np.floor(ground_row))//4
    info = dict(version=26, sample_stride_yx=[4, 4], window_px=64, spacing_px=32,
                model='heldout_reference_texture_and_batched_constrained_light',
                geometry_modified=False, raw_color_modified=False, batched_normal_solves_per_window=5,
                appearance_evidence_is_not_object_absence_proof=True)
    empty_grid = np.zeros((30, 50, 3), dtype=np.float32)
    if np.all(np.ptp(ref.reshape(-1,3), axis=0) < np.array([16, 8, 16])):
        info.update(applied=False, reason='reference_has_no_distinctive_texture',
                    accepted_patches=0, elapsed_host_or_board_s=time.monotonic()-started)
        return reference, empty_grid, info
    models, records = [], []
    verified_samples = np.zeros((120,200),dtype=bool)
    batches = {}
    for cy in range(8,bottom,8):
        for cx in range(8,200,8):
            ya,yb=max(0,cy-8),min(bottom,cy+8)
            xa,xb=max(0,cx-8),min(200,cx+8)
            y,x=yy[ya:yb,xa:xb],xx[ya:yb,xa:xb]
            design=np.stack((np.ones_like(x),(x-cx)/8.,(y-cy)/8.),axis=-1).reshape(-1,3)
            train=((x//2+y//2)%2==0).ravel()
            if min(train.sum(),(~train).sum())<64:continue
            key=(design.shape,design.tobytes(),train.tobytes())
            batches.setdefault(key,[]).append((cx,cy,ya,yb,xa,xb))
    fitted = {}
    for items in [group[k:k+16] for group in batches.values() for k in range(0,len(group),16)]:
        cx,cy,ya,yb,xa,xb=items[0]
        y,x=yy[ya:yb,xa:xb],xx[ya:yb,xa:xb]
        design=np.stack((np.ones_like(x),(x-cx)/8.,(y-cy)/8.),axis=-1).reshape(-1,3)
        train=((x//2+y//2)%2==0).ravel();test=~train
        observed=np.stack([current[a:b,c:d].reshape(-1,3) for _,_,a,b,c,d in items])
        expected=np.stack([ref[a:b,c:d].reshape(-1,3) for _,_,a,b,c,d in items])
        var=np.stack([noise[a:b,c:d].reshape(-1,3) for _,_,a,b,c,d in items])
        mats=np.stack((np.broadcast_to(design,(len(items),3)+design.shape),
                       design[None,None,:,:]*expected.transpose(0,2,1)[:,:,:,None]),axis=1)
        targets=np.stack(((observed-expected).transpose(0,2,1),observed.transpose(0,2,1)),axis=1)
        mt=mats[:,:,:,train,:];tt=targets[:,:,:,train]
        weights=np.ones_like(tt)
        noise_scale=np.sqrt(np.median(var,axis=1))[:,None,:]
        for iteration in range(5):
            gram=np.einsum('bacni,bacn,bacnj->bacij',mt,weights,mt)
            rhs=np.einsum('bacni,bacn,bacn->baci',mt,weights,tt)
            try:
                coefs=np.linalg.solve(gram,rhs[...,None])[...,0]
            except np.linalg.LinAlgError:
                coefs=np.empty_like(rhs)
                for k in range(len(items)):
                    for model_index in range(2):
                        for channel in range(3):
                            coefs[k,model_index,channel]=np.linalg.pinv(gram[k,model_index,channel]).dot(rhs[k,model_index,channel])
            prediction_delta=np.einsum('bacni,baci->bacn',mats,coefs)
            if iteration<4:
                residual=targets-prediction_delta
                scale=np.maximum(np.median(np.abs(residual[:,:,:,train]),axis=3)*1.4826,noise_scale)
                weights=np.minimum(1.,2.5*scale[:,:,:,None]/np.maximum(np.abs(residual[:,:,:,train]),1e-3))
        # Cache the fixed design SVD once per shape rather than twice per patch.
        pinverse=np.linalg.pinv(design[train])
        for k,item in enumerate(items):
            fitted[item[:2]]=(prediction_delta[k],coefs[k],observed[k],expected[k],var[k],design,train,pinverse)
    for cy in range(8,bottom,8):
        for cx in range(8,200,8):
            if (cx,cy) not in fitted:continue
            prediction_delta,coefs,observed,expected,var,design,train,pinverse=fitted[cx,cy]
            test=~train
            ya,yb=max(0,cy-8),min(bottom,cy+8)
            xa,xb=max(0,cx-8),min(200,cx+8)
            y,x=yy[ya:yb,xa:xb],xx[ya:yb,xa:xb]
            predictions=prediction_delta.copy()
            predictions[0]+=expected.T
            train_error=observed[train].T[None,:,:]-predictions[:,:,train]
            losses=np.mean(np.minimum(train_error**2/var[train].T[None,:,:],30.),axis=2)
            gains=np.einsum('ni,ci->cn',design,coefs[1])
            losses[1,~np.all((gains>.25)&(gains<4.),axis=1)]=np.inf
            selected=np.argmin(losses,axis=0)
            coefficients=np.zeros((6,3));predicted=[];selected_models=[]
            for c in range(3):
                kind=int(selected[c]);coef=coefs[kind,c]
                if kind==0:
                    coefficients[:,c]=[1.,coef[0],coef[1],coef[2],0.,0.]
                    selected_models.append('additive_plane')
                else:
                    coefficients[:,c]=[coef[0],0.,0.,0.,coef[1],coef[2]]
                    selected_models.append('multiplicative_plane')
                predicted.append(predictions[kind,c])
            error = observed-np.asarray(predicted).T
            loss = float(np.mean(np.sum(error[test]**2/var[test], axis=1)))
            null = pinverse.dot(observed[train])
            null_error = observed-design.dot(null)
            improvement = np.sum((null_error[test]**2-error[test]**2)/var[test], axis=1)
            ref_plane = pinverse.dot(expected[train])
            detail = expected-design.dot(ref_plane)
            power = float(np.mean(np.sum(detail[test]**2/var[test], axis=1)))
            accepted = bool(loss < 4.5 and power > 2. and float(improvement.mean()) > .75 and
                            float(np.mean(improvement > 0)) > .60 and
                            np.all((coefficients[0] > .25) & (coefficients[0] < 4.)))
            records.append(dict(center_xy=[cx*4, cy*4], accepted=accepted,
                                heldout_loss=loss, reference_texture_power=power,
                                physical_light_models=selected_models,
                                improvement_over_smooth=float(improvement.mean())))
            if accepted:
                models.append((cx, cy, coefficients))
                # Parameter calibration can use matched held-out background
                # samples inside the ROI, not just unrepresentative side bars.
                sample_loss=np.sum(error**2/var,axis=1)
                verified_samples[ya:yb,xa:xb] |= (test & (sample_loss<=12.838)).reshape(y.shape)
    info.update(accepted_patches=len(models), examined_patches=len(records), patches=records)
    if len(models) < 6:
        info.update(applied=False, reason='insufficient_validated_reference_patches',
                    elapsed_host_or_board_s=time.monotonic()-started)
        return reference, empty_grid, info
    weights = np.zeros((120, 200))
    field = np.zeros((120, 200, 3))
    for cx, cy, coef in models:
        # The compact kernel is identically zero outside this square.
        # Accumulate the SAME terms in the SAME model order on its support;
        # avoid hundreds of full-grid temporaries on the PYNQ CPU.
        a,b=max(0,cy-15),min(120,cy+16)
        c,d=max(0,cx-15),min(200,cx+16)
        local_x,local_y=xx[a:b,c:d],yy[a:b,c:d]
        distance = ((local_x-cx)/16.)**2+((local_y-cy)/16.)**2
        weight = np.maximum(0., 1.-distance)**2
        u,v=(local_x-cx)[...,None]/8.,(local_y-cy)[...,None]/8.
        prediction = ((coef[0]-1+u*coef[4]+v*coef[5])*ref[a:b,c:d]+coef[1]+u*coef[2]+v*coef[3])
        field[a:b,c:d] += prediction*weight[..., None]
        weights[a:b,c:d] += weight
    field /= np.maximum(weights[..., None], 1e-9)
    known = weights > 0
    coarse_weight = known.reshape(30, 4, 50, 4).sum(axis=(1, 3))
    coarse = ((field*known[..., None]).reshape(30, 4, 50, 4, 3).sum(axis=(1, 3))/
              np.maximum(coarse_weight[..., None], 1))
    fixed = coarse_weight > 0
    coarse[~fixed] = np.median(coarse[fixed], axis=0)
    for _ in range(200):
        padded = np.pad(coarse, ((1, 1), (1, 1), (0, 0)), mode='edge')
        around = (padded[:-2, 1:-1]+padded[2:, 1:-1]+padded[1:-1, :-2]+padded[1:-1, 2:])*.25
        coarse[~fixed] = around[~fixed]
    field = _interpolate(coarse, (120, 200), 4., 1.5)
    full_field = _interpolate(field, (480, 800), 4.)
    predicted = pack_nearest(rgb565(reference)+full_field)
    predicted[int(np.floor(ground_row)):] = reference[int(np.floor(ground_row)):]
    info.update(applied=True, measured_grid_fraction=float(fixed.mean()),
                smooth_interpolation_fraction=float((~fixed).mean()),
                local_light_delta_p05_p50_p95=np.percentile(full_field[:int(ground_row)], [5, 50, 95], axis=(0, 1)).tolist(),
                elapsed_host_or_board_s=time.monotonic()-started)
    info['verified_background_sample_bits_hex']=np.packbits(verified_samples.ravel()).tobytes().hex()
    info['verified_background_sample_grid_shape']=[120,200]
    info['verified_background_samples']=int(verified_samples.sum())
    return predicted, coarse.astype(np.float32), info


def verified_samples(info):
    value=info.get('verified_background_sample_bits_hex')
    if value is None:
        return None
    return np.unpackbits(np.frombuffer(bytes.fromhex(value),dtype=np.uint8)).reshape(120,200).astype(bool)


def choose_reference_offsets(frame, reference, parameters, ground_row, selection_parameters=None):
    """Learn 0/1-pixel tolerance from known-background EDGE false alarms only.

    This is an empirical nuisance model, not a physical error bound. Never use
    object pixels to choose a radius or expand it until the object passes.
    """
    yy, xx = np.mgrid[4:max(5, int(ground_row)-4):4, 4:796:4]
    anchor = (xx < 160) | (xx >= 640)
    y, x = yy[anchor], xx[anchor]
    cur = rgb565(frame[y, x]).astype(int)
    corrected = np.stack([parameters['lut'][c, cur[:, c]] for c in range(3)], axis=-1).astype(int)
    gx = rgb565(reference[y, x+1])-rgb565(reference[y, x-1])
    gy = rgb565(reference[y+1, x])-rgb565(reference[y-1, x])
    edge = ((np.abs(gx) >= np.array([16, 8, 16])).any(axis=1) |
            (np.abs(gy) >= np.array([16, 8, 16])).any(axis=1))
    def score(dy, dx, configuration=None):
        configuration=parameters if configuration is None else configuration
        ref = rgb565(reference[y+dy, x+dx]).astype(int)
        corrected_input=(corrected if configuration is parameters else
                         np.stack([configuration['lut'][c,cur[:,c]] for c in range(3)],axis=-1).astype(int))
        delta = corrected_input-ref
        return sum(delta[:, c].astype(np.int64)**2 *
                   configuration['inverse_variance'][c, ref[:, c] >> 4] for c in range(3))
    scores = score(0, 0)
    initial = float(np.mean(scores[edge] > parameters['high'])) if edge.any() else 0.
    selection_parameters=parameters if selection_parameters is None else selection_parameters
    selection_scores=score(0,0,selection_parameters)
    selection_error=(float(np.mean(selection_scores[edge]>selection_parameters['high'])) if edge.any() else 0.)
    def result(offsets):
        # Pooling edges into all background hides their larger uncertainty.
        # Calibrate both hysteresis thresholds on this measured stratum too.
        low,high=int(parameters['low']),int(parameters['high'])
        if edge.any():
            low=max(low,int(np.ceil(np.percentile(scores[edge],95))))
            high=max(high,low,int(np.ceil(np.percentile(scores[edge],99.5))))
        if high>0xffffffff:
            raise ValueError('背景边缘变化超过PL评分范围，不能可靠区分前景')
        final=float(np.mean(scores[edge]>high)) if edge.any() else 0.
        return offsets,dict(radius_px=0 if len(offsets)==1 else 1,edge_samples=int(edge.sum()),
                            initial_edge_false_alarm_fraction=initial,
                            selection_edge_false_alarm_fraction=selection_error,
                            final_edge_false_alarm_fraction=final,empirical_target=.005,
                            target_met=final<=.005,recommended_low_q16=low,
                            recommended_high_q16=high,
                            thresholds_calibrated_on_background_edges=True,
                            tolerance_is_not_shape_accuracy_guarantee=True)
    if selection_error <= .005:
        return result([(0,0)])
    offsets = [(0, 0)]+[(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0)]
    for dy, dx in offsets[1:]:
        scores = np.minimum(scores, score(dy, dx))
    return result(offsets)
