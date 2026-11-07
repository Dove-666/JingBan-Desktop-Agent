"""Actual pre-edit module versus new loader, including real eye/part painting."""
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
OUT=ROOT/'artifacts/task-audit/part-alpha8-implementation-20261106'
BACKUP=OUT/'part_interaction.before.py'
OLD_HASH='fa3d2d1ef7e202d619f6d94f42b41878ff47da662c58194283900bcac1208e32'


def fence():
    paths=[ROOT/p for p in ('desktop_pet/part_interaction.py','desktop_pet/eye_tracking.py','desktop_pet/pet_window.py')]
    for directory in ('assets/character/interactions','assets/character/eyes','assets/expressions'):
        paths.extend(p for p in (ROOT/directory).rglob('*') if p.suffix in ('.json','.png'))
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def run():
    from PySide6.QtCore import QCoreApplication,Qt
    from PySide6.QtGui import QImage,QPainter
    from desktop_pet.part_interaction import PartInteraction
    from desktop_pet.eye_tracking import EyeTracker
    from tests.test_interaction_alpha_storage import resident_bytes
    app=QCoreApplication.instance() or QCoreApplication([])
    assert hashlib.sha256(BACKUP.read_bytes()).hexdigest()==OLD_HASH
    name='desktop_pet._owned_alpha_storage_before'
    spec=importlib.util.spec_from_file_location(name,BACKUP)
    old=importlib.util.module_from_spec(spec);sys.modules[name]=old;spec.loader.exec_module(old)
    baseline,candidate,eyes=old.PartInteraction(),PartInteraction(),EyeTracker()
    assert set(baseline.rigs)==set(candidate.rigs)
    byte_totals={'before':resident_bytes(baseline),'after':resident_bytes(candidate)}
    frames=[];alpha_pixels=0;eye_body_frames=0
    def alpha(image):
        raw=bytes(image.constBits());stride=image.bytesPerLine();w,h=image.width(),image.height()
        if image.format()==QImage.Format.Format_Alpha8:return b''.join(raw[y*stride:y*stride+w] for y in range(h))
        assert image.format()==QImage.Format.Format_ARGB32_Premultiplied
        offset=3 if sys.byteorder=='little' else 0
        return b''.join(raw[y*stride+offset:y*stride+w*4:4] for y in range(h))
    def paint(interaction,eye,gaze):
        image=QImage(1024,1024,QImage.Format.Format_ARGB32_Premultiplied);image.fill(Qt.GlobalColor.transparent)
        painter=QPainter(image)
        try:assert interaction.paint(painter,eye_rig=eye,gaze=gaze)
        finally:painter.end()
        return bytes(image.constBits())
    for stem,rig in baseline.rigs.items():
        other=candidate.rigs[stem]
        masks=[(rig.cutout_mask,other.cutout_mask),
            *[(mask,other.repair_masks[key][1]) for key,(_,mask) in rig.repair_masks.items()],
            *[(part.hit_mask,next(p for p in other.parts if p.name==part.name).hit_mask) for part in rig.parts if part.hit_mask is not None]]
        for left,right in masks:
            assert alpha(left)==alpha(right)
            alpha_pixels+=left.width()*left.height()
        baseline.bind_source(rig.source);candidate.bind_source(rig.source);eyes.bind_source(rig.source)
        eye=eyes.rig if eyes.active else None
        names=tuple(sorted(part.name for part in rig.parts));subsets=[()]
        subsets.extend(subset for length in range(1,len(names)+1) for subset in itertools.combinations(names,length))
        for subset in subsets:
            baseline.reset();candidate.reset()
            for part in subset:assert baseline.trigger(part)==candidate.trigger(part)
            baseline.advance(.065);candidate.advance(.065)
            if subset:
                left,right=baseline._body_for_parts(eye,subset),candidate._body_for_parts(eye,subset)
                assert bytes(left.constBits())==bytes(right.constBits())
                eye_body_frames+=1
            for gaze in ((0.,0.),(1.,-1.),(-1.,1.)):
                left,right=paint(baseline,eye,gaze),paint(candidate,eye,gaze)
                assert left==right,(stem,subset,gaze)
                frames.append({'rig':stem,'parts':subset,'gaze':gaze,'matching_eyes':eye is not None,
                    'sha256':hashlib.sha256(right).hexdigest(),'byte_identical':True})
        print(json.dumps({'compared_rig':stem,'matching_eyes':eye is not None,'frames_so_far':len(frames)}),flush=True)
    byte_totals['saved']=byte_totals['before']-byte_totals['after']
    return {'passed':True,'resident_bank_bytes':byte_totals,'exact_alpha_pixels':alpha_pixels,
        'eye_canonical_body_frames':eye_body_frames,'combined_paint_frames':len(frames),'frames':frames,
        'scope':'Actual complete original module from SHA-bound before.py vs actual edited production loader/classes. Real seven interaction rigs + actual EyeTracker bank, rest/all seven moving subsets, three gaze directions. Happy has no matching eye rig and intentionally retains the fallback. Full final ARGB QImage bytes and eye-canonical body bytes match; no QWidget or user profile used. QImage buffers are measured, not process RSS.'}


def main():
    before=fence();blocked=[]
    def deny(*a,**k):blocked.append('network');raise AssertionError('NETWORK_FORBIDDEN')
    try:
        with patch.object(socket,'getaddrinfo',deny),patch.object(socket.socket,'connect',deny),patch.object(socket.socket,'connect_ex',deny):result=run()
    except Exception as error:
        import traceback
        (OUT/'original-compare-error.log').write_text(traceback.format_exc(),encoding='utf8');result={'passed':False,'error_type':type(error).__name__}
    after=fence();result.update(source_before=before,source_after=after,source_unchanged=before==after,
        original_module_sha256=OLD_HASH,blocked_network_attempts=blocked,real_model_requests=0,
        visible_window_created=False,user_profile_used=False,system_clipboard_used=False)
    result['passed']=result['passed'] and before==after and not blocked
    output=OUT/'actual-original-eye-comparison.json';assert not output.exists()
    output.write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({key:value for key,value in result.items() if key not in ('frames','source_before','source_after')},indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
