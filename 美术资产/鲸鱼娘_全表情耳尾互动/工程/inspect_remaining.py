from pathlib import Path
from PIL import Image
import numpy as np
root=Path(__file__).resolve().parent.parent
for state in ['happy','surprised','sleepy','angry','thinking','talk_open']:
 a=np.array(Image.open(root/'透明分层'/state/'base.png').convert('RGBA'))
 mask=np.array(Image.open(root/'透明分层'/state/'cutout_mask.png').convert('RGBA'))[:,:,3]
 x0,y0,x1,y1=605,518,700,546
 roi=a[y0:y1,x0:x1]
 ys,xs=np.where((roi[:,:,3]>8)&(roi[:,:,0]>100)&(roi[:,:,1]>100))
 print(state,'bright base remains',len(xs),'bbox',([int(xs.min()+x0),int(ys.min()+y0),int(xs.max()+x0+1),int(ys.max()+y0+1)] if len(xs) else None))
 print('samples',[(p,a[p[1],p[0]].tolist(),int(mask[p[1],p[0]])) for p in [(610,527),(620,530),(630,533),(640,537),(650,539),(660,537),(670,535),(680,536)]])
