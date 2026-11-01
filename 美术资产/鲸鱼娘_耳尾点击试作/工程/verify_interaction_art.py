"""Read-only QA of Photoshop exports; never modifies pixels."""
from pathlib import Path
from PIL import Image
import numpy as np
import json, hashlib

root = Path(__file__).resolve().parent.parent
repo = root.parent.parent
asset = root / '透明分层'
source_path = repo / 'assets/expressions/neutral.png'
source = np.array(Image.open(source_path).convert('RGBA'))
base = np.array(Image.open(asset/'base.png').convert('RGBA'))
mask = np.array(Image.open(asset/'cutout_mask.png').convert('RGBA'))[:,:,3]
rest = np.array(Image.open(root/'预览/rest_split_transparent.png').convert('RGBA'))
rig = json.loads((asset/'rig.json').read_text(encoding='utf-8-sig'))
outside = mask == 0
visible_diff = np.max(np.abs(rest.astype(int)-source.astype(int)),axis=2)
report = {
 'source_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest(),
 'source_matches_rig': hashlib.sha256(source_path.read_bytes()).hexdigest() == rig['source_sha256'],
 'base_dimensions': list(Image.open(asset/'base.png').size),
 'mask_bbox': Image.fromarray(mask).getbbox(),
 'base_changed_pixels_outside_mask': int(np.any(source != base, axis=2)[outside].sum()),
 'base_alpha_changed_outside_mask': int((source[:,:,3]!=base[:,:,3])[outside].sum()),
 'base_visible_changed_outside_mask': int((np.any(source!=base,axis=2) & ((source[:,:,3]>0)|(base[:,:,3]>0)) & outside).sum()),
 'mask_eye_region_nonzero': int(np.count_nonzero(mask[510:585,380:550])),
 'rest_rgba_different_pixels': int(np.any(rest != source,axis=2).sum()),
 'rest_visible_diff_gt8': int(np.count_nonzero((visible_diff > 8) & ((source[:,:,3]>8)|(rest[:,:,3]>8)))),
 'parts': []
}
for p in rig['parts']:
 im = Image.open(asset/p['image']).convert('RGBA')
 report['parts'].append({'name':p['name'],'size':list(im.size),'alpha_bbox':im.getchannel('A').getbbox(),'origin':p['origin'],'pivot':p['pivot'],'z':p['z']})
print(json.dumps(report,ensure_ascii=False,indent=2))
assert report['source_matches_rig']
assert report['base_dimensions'] == [1024,1024]
assert report['base_alpha_changed_outside_mask'] == 0
assert report['base_visible_changed_outside_mask'] == 0
assert report['mask_eye_region_nonzero'] == 0
assert all(p['size'][0]<200 and p['size'][1]<300 for p in report['parts'])
(root/'工程/art_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
