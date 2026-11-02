"""One dark rim marker: observed same-direction passages, not silhouette FFT.

NumPy/Pillow, Python 3.6. Read-only camera access; no bit/PL/mask changes.
Different DDR arrays are pixel updates, never certified sensor frame ids.
"""
from pathlib import Path
import hashlib
import json
import time
import numpy as np

VERSION=78
REASONS={
    'need_three_full_passages':'尚未看到同一标记连续三次完整经过中线，请检查测速带和标记可见性',
    'period_outside_supported_range':'观测周期超出默认2～90秒范围',
    'passage_intervals_not_stable':'多圈经过时间差仍不稳定，尚不能确认匀速周期',
    'readout_timing_uncertainty_too_large':'当前读图时间的不确定性偏大',
    'too_few_observable_band_updates':'有效的带状画面变化太少，无法可靠确认周期',
    'read_schedule_gap_too_large':'读取过程中出现了较长空档',
    'reader_too_slow_for_24_views':'读一张图耗时相对转速过长，难以可靠采24视角',
    'sampling_too_slow_for_reliable_24_view_timing':'当前测速采样速度相对转速不足'}


class AutoSpeedUncertain(RuntimeError):
    def __init__(self,message,diagnostics):
        super().__init__(message)
        self.diagnostics=diagnostics


def _atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.partial')
    temporary.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),encoding='utf8')
    temporary.replace(path)


def _calibration_id(calibration):
    data={key:float(calibration[key]) for key in ('left_px','right_px','axis_col','v_ground','mm_per_px')}
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode('ascii')).hexdigest()


def measurement_window(calibration,shape=(480,800)):
    height,width=shape
    left,right,axis,ground=[float(calibration[k]) for k in ('left_px','right_px','axis_col','v_ground')]
    if not np.isfinite([left,right,axis,ground]).all() or not 0<=left<axis<right<width:
        raise ValueError('自动测速需要有效的第3步转台标定')
    span=right-left+1
    x0=max(0,int(np.ceil(left+.08*span)));x1=min(width,int(np.floor(right-.08*span))+1)
    y0=int(np.floor(ground))+2;y1=min(height,y0+max(18,min(80,int(np.ceil(.12*span)))))
    if not (0<=y0<y1<=height and y1-y0>=10 and x0<axis<x1):
        raise ValueError('标定台面下方没有足够测速带；请核对第3步标线')
    return (x0,y0,x1,y1)


def _gray565(words):
    r=(words>>11)&31;g=(words>>5)&63;b=words&31
    r=(r<<3)|(r>>2);g=(g<<2)|(g>>4);b=(b<<3)|(b>>2)
    return (r.astype(np.float32)*77+g.astype(np.float32)*150+b.astype(np.float32)*29)/256.


def observe_band(band,window,diameter_px):
    """Local belt brightness removes a smooth gradient; no fixed yellow RGB."""
    x0,y0,x1,y1=window;gray=_gray565(band);h,w=gray.shape
    bins=min(8,max(1,w//16));block=int(np.ceil(w/float(bins)));pad=bins*block-w
    padded=np.pad(gray,((0,0),(0,pad)),mode='edge')
    levels=np.median(padded.reshape(h,bins,block),axis=2)
    centers=(np.arange(bins)+.5)*block-.5
    background=np.empty_like(gray)
    for row in range(h):background[row]=np.interp(np.arange(w),centers,levels[row])
    residual=background-gray
    noise=1.4826*np.median(np.abs(residual-np.median(residual,axis=1)[:,None]),axis=1)
    threshold=np.maximum(np.maximum(8.,.18*background),4*noise[:,None])
    dark=residual>threshold
    minimum_height=max(4,int(.012*diameter_px));maximum_width=max(4,int(.04*diameter_px))
    columns=dark.sum(axis=0)>=minimum_height
    edges=np.flatnonzero(np.diff(np.r_[False,columns,False].astype(np.int8)))
    candidates=[]
    for a,b in zip(edges[::2],edges[1::2]):
        if b-a>maximum_width:continue
        rows=np.flatnonzero(dark[:,a:b].any(axis=1))
        if not len(rows):continue
        top,bottom=int(rows[0]),int(rows[-1])+1
        if bottom-top<minimum_height or bottom-top<.8*(b-a):continue
        weight=np.maximum(residual[top:bottom,a:b],0)*dark[top:bottom,a:b]
        total=float(weight.sum())
        if total<=0:continue
        yy,xx=np.indices(weight.shape)
        cx=x0+a+float((weight*xx).sum()/total);cy=y0+top+float((weight*yy).sum()/total)
        profile=np.array([v.sum() for v in np.array_split(weight.sum(axis=1),8)],float)
        profile/=max(float(np.linalg.norm(profile)),1e-9)
        candidates.append(dict(x=cx,y=cy,width=int(b-a),height=bottom-top,
            contrast=float(total/max(1,dark[top:bottom,a:b].sum())),score=total,
            profile=profile,box=[int(x0+a),int(y0+top),int(x0+b),int(y0+bottom)]))
    candidates=sorted(sorted(candidates,key=lambda v:-v['score'])[:8],key=lambda v:v['x'])
    profile=np.clip(np.maximum(residual,0).mean(axis=0),0,255)
    small=np.interp(np.linspace(0,w-1,128),np.arange(w),profile).astype(np.uint8)
    return candidates,small


class PassageTracker:
    def __init__(self,axis,diameter):
        self.axis=float(axis);self.span=float(diameter);self.tracks=[];self.counter=0

    def update(self,t,candidates,read_uncertainty,band=None):
        self.tracks=[v for v in self.tracks if t-v['obs'][-1][0]<=1.25]
        pairs=[]
        for i,track in enumerate(self.tracks):
            last=track['obs'][-1];dt=t-last[0]
            velocity=0.
            if len(track['obs'])>1:
                previous=track['obs'][-2]
                velocity=(last[1]-previous[1])/max(last[0]-previous[0],1e-9)
            predicted=last[1]+velocity*dt
            reach=min(.25*self.span,.10*self.span+abs(velocity)*dt*.5)
            for j,candidate in enumerate(candidates):
                if abs(candidate['x']-predicted)>reach or abs(candidate['y']-last[2])>max(5.,.035*self.span):continue
                ratio=candidate['height']/max(1.,last[3])
                if not .5<=ratio<=2.:continue
                cost=abs(candidate['x']-predicted)/max(1.,reach)+abs(candidate['y']-last[2])/max(5.,.035*self.span)
                pairs.append((cost,i,j))
        used_tracks=set();used_candidates=set();assignments=[]
        for _,i,j in sorted(pairs):
            if i in used_tracks or j in used_candidates:continue
            used_tracks.add(i);used_candidates.add(j);assignments.append((self.tracks[i],candidates[j]))
        for j,candidate in enumerate(candidates):
            if j not in used_candidates:
                self.counter+=1;track=dict(id=self.counter,obs=[],emitted=False,nearest=None)
                self.tracks.append(track);assignments.append((track,candidate))
        events=[]
        for track,candidate in assignments:
            track['obs'].append((float(t),candidate['x'],candidate['y'],candidate['height'],candidate['width'],float(read_uncertainty)))
            track['obs']=track['obs'][-256:]
            if track['nearest'] is None or abs(candidate['x']-self.axis)<track['nearest']['distance']:
                track['nearest']=dict(distance=abs(candidate['x']-self.axis),candidate=candidate,
                    band=None if band is None else band.copy())
            if track['emitted'] or len(track['obs'])<3:continue
            a=np.asarray(track['obs']);near=np.abs(a[:,1]-self.axis)<=.18*self.span;a=a[near]
            if len(a)<2:continue
            direction=1 if a[-1,1]>a[0,1] else -1
            if direction*(a[0,1]-self.axis)>-.04*self.span or direction*(a[-1,1]-self.axis)<.04*self.span:continue
            if direction*(a[-1,1]-a[0,1])<.10*self.span:continue
            dx=np.diff(a[:,1])*direction
            if float(np.mean(dx>=-1.5))<.8:continue
            crossed=(a[:-1,1]-self.axis)*direction<=0
            crossed &= (a[1:,1]-self.axis)*direction>=0
            crossed &= np.abs(np.diff(a[:,1]))>1e-6
            ids=np.flatnonzero(crossed)
            if not len(ids):continue
            crossings=a[ids,0]+(self.axis-a[ids,1])*np.diff(a[:,0])[ids]/np.diff(a[:,1])[ids]
            crossing=float(np.median(crossings));uncertainty=float(np.max(np.maximum(a[ids,5],a[ids+1,5])))
            local=a[np.abs(a[:,1]-self.axis)<=.08*self.span]
            if len(local)>=4 and local[0,1]!=local[-1,1]:
                origin=float(np.mean(local[:,0]));tt=local[:,0]-origin
                design=np.stack((tt,np.ones(len(tt))),1)
                coefficient=np.linalg.lstsq(design,local[:,1],rcond=-1)[0]
                slope,intercept=coefficient
                if abs(slope)>1e-6 and slope*direction>0:
                    fitted=origin+(self.axis-intercept)/slope
                    error=1.4826*float(np.median(np.abs(local[:,1]-(slope*tt+intercept))))/abs(slope)
                    if a[ids[0],0]<=fitted<=a[ids[-1]+1,0]:crossing=float(fitted)
                    uncertainty=max(uncertainty,error)
            track['emitted']=True;nearest=track['nearest'];c=nearest['candidate']
            event=dict(time_s=crossing,direction=direction,y=float(np.median(a[:,2])),
                height=float(np.median(a[:,3])),width=float(np.median(a[:,4])),
                profile=c['profile'].tolist(),uncertainty_s=max(uncertainty,1e-5),
                observations=len(a),travel_px=float(a[-1,1]-a[0,1]),track_id=track['id'])
            events.append((event,nearest))
        return events


def _same_marker(a,b,span):
    if a['direction']!=b['direction'] or abs(a['y']-b['y'])>max(5.,.03*span):return False
    if not .6<=a['height']/max(b['height'],1.)<=1.67:return False
    if not .45<=a['width']/max(b['width'],1.)<=2.22:return False
    return float(np.dot(a['profile'],b['profile']))>=.75


def estimate_period(events,sample_times,read_times,changed_times,min_period=2.,max_period=90.):
    """Longest stable recent suffix; no harmonic division or silhouette votes."""
    last_reason='need_three_full_passages'
    for count in range(min(6,len(events)),2,-1):
        group=events[-count:];times=np.array([e['time_s'] for e in group]);differences=np.diff(times)
        indices=np.arange(count,dtype=float);indices-=indices.mean()
        period=float(np.dot(indices,times-times.mean())/np.dot(indices,indices))
        tolerance=1e-9*max(1.,abs(period))
        if period<min_period-tolerance or period>max_period+tolerance:last_reason='period_outside_supported_range';continue
        jitter=float(np.max(abs(differences-period))/period)
        if jitter>.02:last_reason='passage_intervals_not_stable';continue
        uncertainty=float(np.sqrt(group[0]['uncertainty_s']**2+group[-1]['uncertainty_s']**2)/(count-1))
        phase_uncertainty=360*uncertainty/period
        if phase_uncertainty>2.5:last_reason='readout_timing_uncertainty_too_large';continue
        observed=np.asarray(sample_times);reads=np.asarray(read_times);updates=np.asarray(changed_times)
        selected=(observed>=times[0])&(observed<=times[-1])
        fresh=updates[(updates>=times[0])&(updates<=times[-1])]
        if len(fresh)<10*(count-1):last_reason='too_few_observable_band_updates';continue
        # A uniform belt can look exactly unchanged while the mark is behind
        # the cylinder. That is not proof of a frozen camera. Bound HOST read
        # gaps; keep pixel-update statistics separate from physical frame ids.
        schedule=observed[selected]
        gap=float(np.max(np.diff(schedule))) if len(schedule)>1 else float('inf')
        if gap>.15*period:last_reason='read_schedule_gap_too_large';continue
        latency=float(np.percentile(reads[selected],95)) if selected.any() else float('inf')
        if latency>.8*period/24.:last_reason='reader_too_slow_for_24_views';continue
        schedule_p95=float(np.percentile(np.diff(schedule),95)) if len(schedule)>1 else float('inf')
        if schedule_p95>.8*period/24.:last_reason='sampling_too_slow_for_reliable_24_view_timing';continue
        return dict(period_s=period,rpm=60./period,intervals_s=differences.tolist(),
            used_passages=group,measured_turns=count-1,relative_interval_variation=jitter,
            empirical_period_uncertainty_s=uncertainty,empirical_phase_uncertainty_deg=phase_uncertainty,
            maximum_read_schedule_gap_s=gap,read_p95_s=latency,sample_interval_p95_s=schedule_p95,
            stable=True,uncertainty_is_not_physical_calibration=True),None
    return None,last_reason


def _save(directory,report,trace,preview=None,window=None):
    if directory is None:return
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    _atomic_json(directory/'1025_marker_speed_v78.json',report)
    if trace:
        path=directory/'1025_marker_speed_trace_v78.npz';temporary=path.with_name(path.name+'.partial')
        with temporary.open('wb') as stream:np.savez_compressed(stream,**trace)
        temporary.replace(path)
    if preview is not None:
        from PIL import Image,ImageDraw
        band,candidate=preview;r=(band>>11)&31;g=(band>>5)&63;b=band&31
        rgb=np.stack(((r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)),-1).astype(np.uint8)
        image=Image.fromarray(rgb);draw=ImageDraw.Draw(image)
        if candidate is not None:
            # PYNQ images can ship Pillow predating the rectangle width keyword.
            a,b,c,d=candidate['box'];draw.rectangle((a-window[0],b-window[1],c-window[0]-1,d-window[1]-1),outline=(0,255,0))
        image.save(str(directory/'1025_marker_speed_preview_v78.png'))


def measure(grab_fn,calibration,directory=None,timeout_s=300.,sample_interval_s=.06,
            clock=time.monotonic,sleeper=time.sleep,progress=None):
    if timeout_s<=0 or sample_interval_s<=0:raise ValueError('测速时限和采样间隔必须为正')
    window=measurement_window(calibration);x0,y0,x1,y1=window
    span=float(calibration['right_px']-calibration['left_px']+1)
    tracker=PassageTracker(calibration['axis_col'],span)
    report=dict(version=VERSION,status='running',method='single_dark_rim_marker_passages',
        window_xyxy=list(window),calibration_sha256=_calibration_id(calibration),
        frame_pixel_order=str(getattr(grab_fn,'frame_pixel_order','unspecified')),
        frame_reader_version=int(getattr(grab_fn,'frame_reader_version',0)),
        one_unique_marker_assumed=True,camera_fixed_assumed=True,
        camera_frame_ownership_verified=False,instantaneous_scan_angles_measured=False,
        average_period_not_encoder=True,period_bounds_s=[2.,90.])
    start=clock();times=[];reads=[];updates=[];profiles=[];profile_times=[];counts=[];clusters=[];events=[]
    previous_band=None;previous_end=start;last_publish=-1.;latest_preview=None;quality_reason='need_three_full_passages'
    try:
        while clock()-start<timeout_s:
            begin=clock();frame=np.asarray(grab_fn());end=clock()
            if frame.dtype!=np.uint16 or frame.shape!=(480,800):raise ValueError('测速需要480x800 uint16 RGB565摄像头原图')
            band=np.array(frame[y0:y1,x0:x1],copy=True)
            changed=previous_band is None or not np.array_equal(band,previous_band)
            sample=.5*(begin+end)-start
            times.append(sample);reads.append(end-begin)
            if changed:
                # Bracket the observed buffer update, not a claimed exposure.
                t=.5*(previous_end+end)-start
                uncertainty=max(.5*(end-previous_end),.5*(end-begin))
                candidates,profile=observe_band(band,window,span)
                updates.append(t);profiles.append(profile);profile_times.append(t);counts.append(len(candidates))
                if latest_preview is None:latest_preview=(band.copy(),candidates[0] if candidates else None)
                for event,nearest in tracker.update(t,candidates,uncertainty,band):
                    events.append(event)
                    matching=[v for v in clusters if _same_marker(event,v['template'],span)]
                    if matching:cluster=min(matching,key=lambda v:abs(event['height']-v['template']['height']))
                    elif len(clusters)<12:
                        cluster=dict(template=event,events=[]);clusters.append(cluster)
                    else:continue
                    cluster['events'].append(event)
                    quality,reason=estimate_period(cluster['events'],times,reads,updates)
                    quality_reason=reason or 'accepted'
                    if quality is not None:
                        report.update(quality,status='accepted',elapsed_s=end-start,read_samples=len(times),
                            distinct_adjacent_band_samples=len(updates),all_passages=events,
                            marker_clusters=len(clusters),discarded_earlier_passages=len(cluster['events'])-len(quality['used_passages']))
                        identity=json.dumps(dict(period_s=report['period_s'],passages=report['used_passages'],calibration=report['calibration_sha256']),sort_keys=True)
                        report['measurement_id']=hashlib.sha256(identity.encode('utf8')).hexdigest()
                        trace=dict(sample_times_s=np.asarray(times),read_durations_s=np.asarray(reads),
                            changed_band_times_s=np.asarray(profile_times),dark_profiles128=np.asarray(profiles,np.uint8),candidate_counts=np.asarray(counts,np.int16))
                        latest_preview=(nearest['band'],nearest['candidate'])
                        _save(directory,report,trace,latest_preview,window)
                        return report
                previous_band=band
            previous_end=end
            if progress is not None and end-start-last_publish>=1.:
                try:
                    progress(dict(elapsed_s=end-start,passages=len(events),
                        best_matching_passages=max([len(v['events']) for v in clusters] or [0]),
                        read_samples=len(times),observable_band_updates=len(updates),quality_reason=quality_reason))
                except Exception as error:
                    report['progress_display_error']=str(error)
                last_publish=end-start
            remaining=sample_interval_s-(clock()-begin)
            if remaining>0:sleeper(remaining)
        report.update(status='uncertain',reason=quality_reason,elapsed_s=clock()-start,
            read_samples=len(times),distinct_adjacent_band_samples=len(updates),all_passages=events,marker_clusters=len(clusters))
        trace=dict(sample_times_s=np.asarray(times),read_durations_s=np.asarray(reads),
            changed_band_times_s=np.asarray(profile_times),dark_profiles128=np.asarray(profiles,np.uint8).reshape(-1,128),candidate_counts=np.asarray(counts,np.int16))
        _save(directory,report,trace,latest_preview,window)
        raise AutoSpeedUncertain('未获得可靠的黑标记整圈周期：'+REASONS.get(quality_reason,quality_reason),report)
    except KeyboardInterrupt:
        report.update(status='interrupted',elapsed_s=clock()-start,read_samples=len(times),all_passages=events)
        _save(directory,report,None,latest_preview,window)
        raise


def measure_period_demo(grab_fn,calibration,directory,ask=input,manual_measure=None,**kwargs):
    """Normal route needs no Enter timing; retry/manual/cancel only on uncertainty."""
    handle=None
    try:
        from IPython import get_ipython
        from IPython.display import display
        if get_ipython() is not None:handle=display('自动测速：等待黑色标记经过转台中部……',display_id=True)
    except Exception:pass
    print('自动跟踪胶带上的唯一黑标记；正常无需按回车，连续观察至少两圈。',flush=True)
    def publish(state):
        text='自动测速：同一标记已通过%d次｜已用%.0f秒'%(state['best_matching_passages'],state['elapsed_s'])
        if handle is not None:
            try:handle.update(text)
            except Exception:pass
    while True:
        try:
            result=measure(grab_fn,calibration,directory,progress=publish,**kwargs)
            if handle is not None:
                try:handle.update('自动测速完成')
                except Exception:pass
            print('自动实测 %.3f 秒/圈，%.2f rpm；%d圈间隔最大偏差 %.2f%%。'%(result['period_s'],result['rpm'],result['measured_turns'],100*result['relative_interval_variation']),flush=True)
            return result['period_s'],result
        except AutoSpeedUncertain as error:
            print(str(error),flush=True)
            print('请查看本次目录的1025_marker_speed_preview_v78.png和测速记录。',flush=True)
            choice=ask('输入r重试自动测速，m使用原手动计时，q取消：').strip().lower()
            while choice not in ('r','m','q'):
                choice=ask('请输入r、m或q：').strip().lower()
            if choice=='r':continue
            if choice=='q':return None,error.diagnostics
            if manual_measure is None:
                from scan_pipeline import measure_period
                manual_measure=measure_period
            value=manual_measure(turns=3,ask=ask)
            if value is None:return None,error.diagnostics
            result=dict(error.diagnostics,status='manual_fallback',method='manual_three_turn_timing',
                period_s=float(value),rpm=60./float(value),auto_reason=error.diagnostics.get('reason'))
            result['measurement_id']=hashlib.sha256(json.dumps(result,sort_keys=True).encode('utf8')).hexdigest()
            _atomic_json(Path(directory)/'1025_marker_speed_v78.json',result)
            return float(value),result


def validate_for_capture(speed_info,period_s,calibration=None):
    """Refuse stale/mismatched measurement before spending a turn on photos."""
    if not isinstance(speed_info,dict) or speed_info.get('period_s') is None:return
    actual=float(period_s);measured=float(speed_info['period_s'])
    if not np.isfinite([actual,measured]).all() or abs(actual-measured)>1e-9*max(1.,abs(measured)):
        raise ValueError('本次照片的周期与测速记录不匹配')
    if calibration is not None and _calibration_id(calibration)!=speed_info['calibration_sha256']:
        raise ValueError('测速后标定已改变，请重新运行第5步')


def attach_to_capture(color_scan,speed_info,directory,calibration=None):
    """Small sidecar links existing NPZ photos without recompressing 24 frames."""
    if not isinstance(speed_info,dict) or speed_info.get('period_s') is None:return
    validate_for_capture(speed_info,color_scan['period_s'],calibration)
    actual=float(color_scan['period_s'])
    frames=np.asarray(color_scan['frames565'])
    report=dict(version=VERSION,measurement_id=speed_info.get('measurement_id'),
        period_s=actual,method=speed_info['method'],speed_record_file='1025_marker_speed_v78.json',
        frames_sha256=hashlib.sha256(frames.astype('<u2',copy=False).tobytes()).hexdigest(),
        speed_measurement=speed_info)
    _atomic_json(Path(directory)/'1014_color24_v1_speed_source.json',report)
    color_scan['rotation_measurement_json']=json.dumps(report,ensure_ascii=False,separators=(',',':'))
