"""Alternate algorithm: PL evidence/edges, sparse witnesses, PS min-cut."""
import collections
import time
import numpy as np
VERSION=46


def _regions_reference(state,horizontal,vertical,max_runs=150000):
    h,w=state.shape;parent=[];rows=[];labels=np.empty((h,w),np.int32)
    def root(a):
        while parent[a]!=a:
            parent[a]=parent[parent[a]];a=parent[a]
        return a
    for y in range(h):
        cuts=np.flatnonzero(horizontal[y,:-1]|(state[y,1:]!=state[y,:-1]))+1
        starts=np.r_[0,cuts];ends=np.r_[cuts,w];current=[]
        for a,b in zip(starts,ends):
            i=len(parent);parent.append(i);current.append((int(a),int(b),i));labels[y,a:b]=i
        if len(parent)>max_runs:raise ValueError('source edge complexity exceeds bounded alternate algorithm')
        if y:
            i=j=0;previous=rows[-1]
            while i<len(current) and j<len(previous):
                a,b,k=current[i];c,d,l=previous[j];lo,hi=max(a,c),min(b,d)
                if lo<hi and state[y,a]==state[y-1,c] and (~vertical[y-1,lo:hi]).any():
                    ra,rb=root(k),root(l)
                    if ra!=rb:parent[rb]=ra
                if b<=d:i+=1
                else:j+=1
        rows.append(current)
    mapping=np.asarray([root(i) for i in range(len(parent))],int)
    _,mapping=np.unique(mapping,return_inverse=True)
    return mapping[labels]


try:
    from scipy.ndimage import label as _compiled_region_label
except (ImportError,OSError):
    _compiled_region_label=None
REGION_ACCELERATION_BACKEND='scipy_ndimage' if _compiled_region_label is not None else 'original_python'
PERFORMANCE_VERSION=82


def regions(state,horizontal,vertical,max_runs=150000):
    # Existing readers provide Boolean PL edge arrays. Preserve the original
    # path for unsupported input layouts/types and when SciPy is unavailable.
    h,w=state.shape
    if (_compiled_region_label is None or not h or not w or
            np.asarray(horizontal).dtype!=np.dtype(bool) or np.asarray(vertical).dtype!=np.dtype(bool)):
        return _regions_reference(state,horizontal,vertical,max_runs)
    starts=np.ones((h,w),bool)
    starts[:,1:]=horizontal[:,:-1]|(state[:,1:]!=state[:,:-1])
    run_count=int(starts.sum())
    if run_count>max_runs:raise ValueError('source edge complexity exceeds bounded alternate algorithm')
    # Pixel nodes occupy even/even coordinates. Each intermediate edge is
    # present iff the original run union would permit exactly that adjacency.
    graph=np.zeros((2*h-1,2*w-1),bool);graph[::2,::2]=True
    graph[::2,1::2]=~horizontal[:,:-1]&(state[:,1:]==state[:,:-1])
    graph[1::2,::2]=~vertical[:-1,:]&(state[1:,:]==state[:-1,:])
    components,count=_compiled_region_label(graph,structure=np.array([[0,1,0],[1,1,1],[0,1,0]],np.uint8))
    labels=components[::2,::2]
    # Reference union: each current-row run attaches all previous roots to
    # itself. Therefore a final component's root is its greatest run index.
    # Sorting those exact roots restores original IDs, not merely a visually
    # equivalent partition (IDs affect ordered sampling and tied budgets).
    maxima=np.full(count+1,-1,np.int64)
    np.maximum.at(maxima,labels[starts],np.arange(run_count,dtype=np.int64))
    order=np.argsort(maxima[1:],kind='mergesort')+1
    remap=np.empty(count+1,np.intp);remap[0]=-1;remap[order]=np.arange(count,dtype=np.intp)
    return remap[labels]


class Flow:
    def __init__(self,n):self.edges=[[] for _ in range(n)]
    def add(self,a,b,c):
        u=[b,float(c),len(self.edges[b])];v=[a,0.,len(self.edges[a])]
        self.edges[a].append(u);self.edges[b].append(v)
    def solve(self,s,t):
        n=len(self.edges);augmentations=0
        while True:
            level=[-1]*n;level[s]=0;q=collections.deque([s])
            while q:
                a=q.popleft()
                for b,c,_ in self.edges[a]:
                    if c>1e-9 and level[b]<0:level[b]=level[a]+1;q.append(b)
            if level[t]<0:break
            pointers=[0]*n
            while True:
                nodes=[s];path=[]
                while nodes and nodes[-1]!=t:
                    a=nodes[-1]
                    while pointers[a]<len(self.edges[a]):
                        b,c,_=self.edges[a][pointers[a]]
                        if c>1e-9 and level[b]==level[a]+1:break
                        pointers[a]+=1
                    if pointers[a]==len(self.edges[a]):
                        level[a]=-1;nodes.pop()
                        if path:path.pop()
                    else:
                        path.append((a,pointers[a]));nodes.append(self.edges[a][pointers[a]][0])
                if not nodes:break
                augmentations+=1
                if augmentations>100000:raise ValueError('binary graph work budget exceeded')
                amount=min(self.edges[a][i][1] for a,i in path)
                for a,i in path:
                    b,_,reverse=self.edges[a][i]
                    self.edges[a][i][1]-=amount;self.edges[b][reverse][1]+=amount
        seen={s};q=collections.deque([s])
        while q:
            for b,c,_ in self.edges[q.popleft()]:
                if c>1e-9 and b not in seen:seen.add(b);q.append(b)
        return seen


def partition(device,frame,reference,lut,ground,noise=None,progress=None):
    import background_witness
    invalid=(np.asarray(frame).dtype!=np.uint16 or np.asarray(reference).dtype!=np.uint16 or
             np.asarray(frame).shape!=(480,800) or np.asarray(reference).shape!=(480,800) or
             np.asarray(lut).shape!=(3,256) or np.asarray(lut).dtype!=np.uint8 or
             not np.isfinite(ground) or not 8<ground<=480)
    if noise is not None:
        a=np.asarray(noise);invalid |= a.shape!=(480,800,3) or not np.isfinite(a).all() or (a<0).any()
    if invalid:
        error=ValueError('边缘图分割的原图、响应表、台面或噪声输入无效')
        error.edge_partition_input_failure=True
        raise error
    start=time.monotonic();bottom=int(np.floor(ground));identity=np.tile(np.arange(256,dtype=np.uint8),(3,1))
    cfg=dict(lut=lut,inverse_variance=np.tile(np.asarray([1024,4096,1024],np.uint16)[:,None],(1,16)),
             low=int(np.ceil(7.815*65536)),high=int(np.ceil(12.838*65536)))
    statuses=[];words=[];references=[];configurations=[]
    evidence=dict(status=statuses,packed=words,references=references,configurations=configurations)
    def feature(ref,parameters,kind):
        references.append(ref.copy());configurations.append(parameters)
        if progress:progress.update('PL颜色与边缘证据',kind)
        try:
            a,b,st,wp=device.classify(frame,ref,parameters)
        except BaseException as exc:
            exc.edge_partition_evidence=evidence
            exc.edge_partition_hardware_failure=True
            raise
        statuses.append(st);words.append(wp.copy())
        return a,b
    s,p=feature(reference,cfg,'颜色差异 1/3')
    gradients=[]
    for axis in (1,0):
        other=np.empty_like(frame)
        if axis==1:other[:,:-1]=frame[:,1:];other[:,-1]=frame[:,-1]
        else:other[:-1]=frame[1:];other[-1]=frame[-1]
        gs,gp=feature(other,dict(cfg,lut=identity,low=int(np.ceil((81./64*2+25./16)*65536)),
                                _within_reference_group=axis==0),'图像边缘 %d/3'%(2 if axis==1 else 3))
        gradients.append((gs[:bottom],gp[:bottom]))
    state=s[:bottom].astype(np.uint8)*2+p[:bottom].astype(np.uint8)
    if progress:progress.update('PS二值边缘区域划分')
    try:
        labels=regions(np.zeros_like(state),gradients[0][1],gradients[1][1]);n=int(labels.max())+1
        confidence_refinement=n>1024
        if confidence_refinement:
            # Real sensor texture can break source-edge contours. Split those
            # components with measured PL confidence before any BG witness
            # is allowed to represent an entire connected source region.
            labels=regions(state,gradients[0][1],gradients[1][1]);n=int(labels.max())+1
        if n>16384:raise ValueError('refined region count exceeds bounded alternate graph budget')
    except ValueError as exc:
        exc.edge_partition_evidence=evidence
        raise
    flat=labels.ravel();area=np.bincount(flat,minlength=n)
    order=np.argsort(flat,kind='mergesort');ends=np.cumsum(area);starts=ends-area
    coords=[];owners=[]
    for i,(a,b) in enumerate(zip(starts,ends)):
        selected=order[a+np.linspace(0,b-a-1,min(4,b-a)).astype(int)]
        coords.extend((int(k//800),int(k%800)) for k in selected);owners.extend([i]*len(selected))
    if progress:progress.update('稀疏背景纹理见证')
    witness_stats={}
    compatible=background_witness.background_compatible(frame,reference,lut,np.asarray(coords),noise,radius=4,fractional=True,progress=progress,stats=witness_stats)
    if not np.array_equal(lut,identity):
        unresolved=np.flatnonzero(~compatible)
        if len(unresolved):
            compatible[unresolved] |= background_witness.background_compatible(frame,reference,identity,np.asarray(coords)[unresolved],noise,radius=4,fractional=True,progress=progress,stats=witness_stats)
    # Propagate only a measured conditional BG color pair, not an arbitrary
    # mask label. This gives a narrow opening a witness even when every patch
    # around it also sees foreground. All centers must match verified samples.
    cy,cx=np.asarray(coords).T
    ref_rgb=background_witness.rgb565(reference[cy,cx])
    cur_raw=background_witness.rgb565(frame[cy,cx]).astype(int)
    cur_rgb=np.stack([lut[c,cur_raw[:,c]] for c in range(3)],1)
    paired=background_witness.conditional_pairs(cx,ref_rgb,cur_rgb,compatible)
    tested=np.bincount(owners,minlength=n);supported=np.bincount(owners,weights=compatible,minlength=n)
    bg=(supported>=np.minimum(tested,2))&(supported>=tested*.5)
    pair_all=np.ones(n,bool);np.logical_and.at(pair_all,np.asarray(owners),paired)
    bg |= pair_all
    positive=np.bincount(flat,weights=s[:bottom].ravel(),minlength=n)
    weak=np.bincount(flat,weights=(p[:bottom]&~s[:bottom]).ravel(),minlength=n)
    negative=np.maximum(0,area-positive-weak)
    fg_cost=np.where(bg,area.astype(float)*8.,-np.log(.05)*negative)
    bg_cost=np.where(bg,0.,-np.log(.005)*positive-np.log(.05)*weak)
    flow=Flow(n+2);source=n;sink=n+1
    for i in range(n):flow.add(source,i,bg_cost[i]);flow.add(i,sink,fg_cost[i])
    pairs=[];weights=[];quiet=-np.log(.005)/2
    for axis,(gs,gp) in enumerate(gradients):
        if axis==0:a,b=labels[:,:-1],labels[:,1:];gs,gp=gs[:,:-1],gp[:,:-1]
        else:a,b=labels[:-1],labels[1:];gs,gp=gs[:-1],gp[:-1]
        hit=a!=b;lo=np.minimum(a[hit],b[hit]);hi=np.maximum(a[hit],b[hit])
        weight=np.where(gs[hit],quiet*np.exp(-12.838/2),np.where(gp[hit],quiet*np.exp(-(81./64*2+25./16)/2),quiet))
        pairs.append(lo*n+hi);weights.append(weight)
    keys=np.concatenate(pairs);values=np.concatenate(weights);unique,index=np.unique(keys,return_inverse=True)
    energy=np.bincount(index,weights=values)
    for k,w in zip(unique,energy):
        a,b=int(k//n),int(k%n);flow.add(a,b,w);flow.add(b,a,w)
    if progress:progress.update('PS二值区域图优化')
    try:reachable=flow.solve(source,sink)
    except ValueError as exc:
        exc.edge_partition_evidence=evidence
        raise
    foreground=np.array([i in reachable for i in range(n)])
    body=np.zeros((480,800),bool);body[:bottom]=foreground[labels]
    crop=body[0,160:640].any() or body[:bottom,160].any() or body[:bottom,639].any()
    result=np.zeros_like(body);result[:bottom,160:640]=body[:bottom,160:640]
    info=dict(algorithm='PL_quantized_colour_and_source_edges_then_PS_binary_graph_cut',
        witness_work_v46=witness_stats,regions=n,confidence_region_refinement=bool(confidence_refinement),region_pairs=len(unique),patch_centers=len(coords),PL_feature_passes=3,
        region_areas=area.tolist(),region_background_witness=bg.tolist(),witness_support=supported.tolist(),witness_tested=tested.tolist(),
        witness_pixels_yx=[list(c) for c in coords],witness_background_compatible=compatible.tolist(),
        conditional_pair_background_compatible=paired.tolist(),region_foreground=foreground.tolist(),
        foreground_pixels=int(result.sum()),boundary_requires_review=bool(crop),
        sufficient_foreground_evidence=bool(result.sum()>=150),
        elapsed_host_or_board_s=time.monotonic()-start,PL_feature_status=statuses,
        morphological_hole_filling=False,object_class_prior=False)
    evidence.update(parameters=cfg,strong=s,possible=p,labels=labels,full_foreground=body)
    return result,info,evidence
