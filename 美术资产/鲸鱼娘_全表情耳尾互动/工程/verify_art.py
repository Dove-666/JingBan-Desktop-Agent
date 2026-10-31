"""Read-only validation of real Photoshop artifacts and protected originals."""
from pathlib import Path
import hashlib, json
import numpy as np
from PIL import Image

work=Path(__file__).resolve().parent
root=work.parent
repo=root.parent.parent
before=json.loads((work/'protected_before.json').read_text(encoding='utf-8-sig'))
changes=[]
for item in before:
 p=Path(item['path'])
 if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']: changes.append(str(p))
states=['neutral','happy','surprised','sleepy','angry','thinking','talk_open']
reports={}
for state in states:
 path=root/'透明分层'/state
 rig=json.loads((path/'rig.json').read_text(encoding='utf-8-sig'))
 source_path=repo/f'assets/expressions/{state}.png'
 source=np.array(Image.open(source_path).convert('RGBA'))
 base=np.array(Image.open(path/'base.png').convert('RGBA'))
 mask=np.array(Image.open(path/'cutout_mask.png').convert('RGBA'))[:,:,3]
 rest=np.array(Image.open(root/f'预览/{state}/rest_split_transparent.png').convert('RGBA'))
 outside=mask==0
 visible=(source[:,:,3]>0)|(base[:,:,3]>0)
 report={'source_sha256':hashlib.sha256(source_path.read_bytes()).hexdigest(),
  'rig_source_matches':hashlib.sha256(source_path.read_bytes()).hexdigest()==rig['source_sha256'],
  'base_size':list(Image.open(path/'base.png').size),
  'base_visible_changed_outside_mask':int((np.any(source!=base,axis=2)&visible&outside).sum()),
  'base_alpha_changed_outside_mask':int(((source[:,:,3]!=base[:,:,3])&outside).sum()),
  'hidden_rgb_changed_outside_mask':int((np.any(source!=base,axis=2)&~visible&outside).sum()),
  'eye_region_mask_nonzero':int(np.count_nonzero(mask[500:590,375:560])),
  'rest_visible_rgba_diff_gt8':int(((np.max(abs(rest.astype(int)-source.astype(int)),axis=2)>8)&((source[:,:,3]>8)|(rest[:,:,3]>8))).sum()),
  'native_psd_bytes':(root/f'PSD/{state}_interaction_rig.psd').stat().st_size,
  'parts':[]}
 for part in rig['parts']:
  im=Image.open(path/part['image']).convert('RGBA')
  hit=Image.open(path/part['hit_mask']).convert('RGBA')
  info={'name':part['name'],'image_size':list(im.size),'origin':part['origin'],'pivot':part['pivot'],'amplitude_degrees':part['amplitude_degrees'],'z':part['z'],'hit_mask_size':list(hit.size),'hit_alpha_bbox':hit.getchannel('A').getbbox()}
  report['parts'].append(info)
  assert im.width<200 and im.height<300,(state,part['name'])
  assert hit.size==(1024,1024),(state,part['name'],'hit size')
  assert part['z']=='behind'
 assert report['rig_source_matches'] and report['base_size']==[1024,1024]
 assert report['base_visible_changed_outside_mask']==0
 assert report['base_alpha_changed_outside_mask']==0
 assert report['eye_region_mask_nonzero']==0
 assert report['native_psd_bytes']>500000
 reports[state]=report
result={'protected_existing_files':len(before),'protected_changed_files':changes,'states':reports}
(work/'art_verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
assert not changes,changes
print(json.dumps({'protected':len(before),'changed':changes,'states':{state:{'source':data['source_sha256'],'rest_diff_gt8':data['rest_visible_rgba_diff_gt8'],'visible_changed_outside_mask':data['base_visible_changed_outside_mask']} for state,data in reports.items()}},indent=2))
