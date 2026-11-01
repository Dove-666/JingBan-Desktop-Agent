"""Read-only source inspection; writes coordinate JSON, never edits image pixels."""
from pathlib import Path
import re, ast, json, copy
from PIL import Image
import numpy as np

work = Path(__file__).resolve().parent
repo = work.parent.parent.parent
old = repo/'美术资产/鲸鱼娘_耳尾点击试作/工程/build_interaction_rig.jsx'
text = old.read_text(encoding='utf-8')
block = re.search(r'var parts=\[([\s\S]*?)\n\];',text).group(1)
parts = []
for entry in re.split(r'\n \{name:',block)[1:]:
    name = re.match(r"'([^']+)'",entry).group(1)
    part = {'name':name}
    for field in ['pivot','shape','blue','white']:
        match = re.search(r'\b'+field+r':(\[\[[^;]*?\]\]|\[[^\[\]]*\])(?=,|})',entry)
        if match: part[field] = ast.literal_eval(match.group(1))
    for field in ['blueHex','whiteHex']:
        match = re.search(field+r":'([^']+)'",entry)
        if match: part[field] = match.group(1)
    part['amplitude'] = int(re.search(r'amplitude:(\d+)',entry).group(1))
    parts.append(part)
assert len(parts)==3 and len(parts[2]['shape'])>50
ref = np.array(Image.open(repo/'assets/expressions/neutral.png').convert('RGBA'))
states = ['happy','surprised','sleepy','angry','thinking','talk_open']
configs, audit = {}, {}
boxes = {'left_ear':[225,485,315,570], 'right_ear':[600,452,741,541], 'tail':[666,595,814,864]}
def edge(a,x,y):
    x,y=round(x),round(y)
    lo,hi=x-5,x+6
    lum=a[y,lo:hi,:3].astype(int).sum(axis=1)
    near=np.flatnonzero(lum <= lum.min()+20)+lo
    return float(near.mean())
for state in states:
    a = np.array(Image.open(repo/f'assets/expressions/{state}.png').convert('RGBA'))
    config = copy.deepcopy(parts)
    adjustments=[]
    # The neutral trial's generous ear-bottom padding included a thin strip of
    # blue hair. Isolate this expression's true pale fin + dark outline instead.
    # Use the source only to measure coordinates; all pixel operations are PS.
    fin_edge=[]
    for x in range(668,609,-1):
        col=a[510:542,x]
        whites=np.flatnonzero((col[:,:3].min(axis=1)>125)&(col[:,3]>200))
        if not len(whites): raise ValueError((state,'no pale ear at',x))
        w=int(whites[-1]+510)
        lo,hi=w+1,min(w+5,547)
        lum=a[lo:hi,x,:3].astype(int).sum(axis=1)
        black=float((np.flatnonzero(lum<=lum.min()+15)+lo).mean())
        fin_edge.append([x,round(black+0.65,2)])
    config[1]['shape']=config[1]['shape'][:18]+fin_edge+config[1]['shape'][27:]
    config[1]['clear_shape']=copy.deepcopy(config[1]['shape'])
    for index in range(12,18): config[1]['clear_shape'][index][1]+=3
    for index in range(18,18+len(fin_edge)):
        # Background side gets extra coverage; the adjacent hair is preserved.
        if config[1]['clear_shape'][index][0]>=660: config[1]['clear_shape'][index][1]+=3
    # Follow each expression's own stationary black hair outline. The tested
    # neutral curve remains the reference, including [695,660] -> [679,668]
    # -> [731,720] -> [751,760]; only subpixel X deltas are applied.
    for index in range(1,30):
        x,y = config[2]['shape'][index]
        dx = max(-3.0,min(3.0,edge(a,x-3,y)-edge(ref,x-3,y)))
        config[2]['shape'][index][0] = round(x+dx,2)
        if dx: adjustments.append({'index':index,'reference':[x,y],'dx':dx})
    for part,point in zip(config,[(290,520),(650,485),(700,825)]):
        x,y=point
        rgb=np.median(a[y-1:y+2,x-1:x+2,:3],axis=(0,1)).astype(int)
        part['blueHex']=''.join(f'{v:02X}' for v in rgb)
    configs[state]=config
    regions={}
    for name,(x0,y0,x1,y1) in boxes.items():
        aa=a[y0:y1,x0:x1,3]>8; rr=ref[y0:y1,x0:x1,3]>8
        regions[name]={'alpha_iou':float((aa&rr).sum()/(aa|rr).sum()),'alpha_different_pixels':int((aa!=rr).sum())}
    audit[state]={'alpha_bbox':Image.fromarray(a[:,:,3]).getbbox(),'regions':regions,'tail_inner_edge_adjustments':adjustments,'right_ear_fin_lower_edge':fin_edge}
(work/'geometry.json').write_text(json.dumps(configs,indent=2),encoding='utf-8')
(work/'geometry_inspection.json').write_text(json.dumps(audit,indent=2),encoding='utf-8')
print('6 state-specific Photoshop coordinate configs, read-only geometry inspection complete')
