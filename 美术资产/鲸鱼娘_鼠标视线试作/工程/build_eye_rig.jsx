#target photoshop
(function () {
    var work = File($.fileName).parent, root = work.parent;
    var assets = Folder(root.fsName + '/\u900f\u660e\u5206\u5c42');
    var previews = Folder(root.fsName + '/\u9884\u89c8');
    var psds = Folder(root.fsName + '/PSD');
    assets.create(); previews.create(); psds.create();
    var log = File(work.fsName + '/photoshop_eye_rig.log'); log.encoding = 'UTF8'; log.open('a');
    function record(s) { log.writeln(new Date() + ' ' + s); log.close(); log.open('a'); }
    function cID(s) { return charIDToTypeID(s); }
    var oldDialogs = app.displayDialogs, oldUnits = app.preferences.rulerUnits;
    app.displayDialogs = DialogModes.NO; app.preferences.rulerUnits = Units.PIXELS;
    function png(doc, file) { var o = new PNGSaveOptions(); o.compression = 9; doc.saveAs(file, o, true, Extension.LOWERCASE); }
    function bounds(layer) { var b=layer.bounds; return [b[0].as('px'),b[1].as('px'),b[2].as('px'),b[3].as('px')]; }
    function json(v) {
        if (v === null) return 'null';
        if (typeof v === 'number' || typeof v === 'boolean') return String(v);
        if (typeof v === 'string') return '"' + v.replace(/\\/g,'\\\\').replace(/"/g,'\\"').replace(/\r/g,'\\r').replace(/\n/g,'\\n') + '"';
        var p=[], i;
        if (v instanceof Array) { for(i=0;i<v.length;i++)p.push(json(v[i]));return '['+p.join(',')+']'; }
        for(i in v)if(v.hasOwnProperty(i))p.push(json(i)+':'+json(v[i])); return '{'+p.join(',')+'}';
    }
    function write(file, text) { file.encoding='UTF8';file.open('w');file.write(text);file.close(); }
    /* Each knot is [anchor, incoming Bezier handle, outgoing Bezier handle]. */
    function path(doc, name, knots) {
        /* Sample cubic Beziers into an antialiased Photoshop polygon selection.
           Do not leave a vector path active: Photoshop's Clear command otherwise
           edits path nodes instead of raster pixels on some tool states. */
        var points=[];
        for(var i=0;i<knots.length;i++) {
            var a=knots[i],b=knots[(i+1)%knots.length];
            for(var j=0;j<20;j++) {
                var t=j/20,u=1-t;
                points.push([u*u*u*a[0][0]+3*u*u*t*a[2][0]+3*u*t*t*b[1][0]+t*t*t*b[0][0],u*u*u*a[0][1]+3*u*u*t*a[2][1]+3*u*t*t*b[1][1]+t*t*t*b[0][1]]);
            }
        }
        return points;
    }
    function choose(doc, shape) { app.activeDocument=doc;doc.selection.select(shape,SelectionType.REPLACE,0,true); }
    function selectRGB() { var d=new ActionDescriptor(),r=new ActionReference();r.putEnumerated(cID('Chnl'),cID('Chnl'),cID('RGB '));d.putReference(cID('null'),r);executeAction(cID('slct'),d,DialogModes.NO); }
    function revealSelectionMask() {
        var d=new ActionDescriptor();d.putClass(cID('Nw  '),cID('Chnl'));
        var r=new ActionReference();r.putEnumerated(cID('Chnl'),cID('Chnl'),cID('Msk '));d.putReference(cID('At  '),r);
        d.putEnumerated(cID('Usng'),cID('UsrM'),cID('RvlS'));executeAction(cID('Mk  '),d,DialogModes.NO);
    }
    var eyes=[
        {name:'left',center:[414.5,554.4],travel:[8,5],
         iris:[[[411.1,532.9],[402.6,533.5],[421.8,534.0]],[[434.5,556.4],[434.2,545.8],[435.5,567.8]],[[416.1,576.4],[426.6,576.2],[405.1,577.0]],[[395.4,554.2],[395.3,565.0],[395.1,542.8]]],
         aperture:[[[406.8,533.3],[398.5,535.5],[415.5,531.6]],[[429.3,544.9],[425.8,539.0],[434.0,552.2]],[[435.2,558.4],[435.2,552.5],[435.5,566.0]],[[414.4,577.2],[428.7,577.6],[402.7,578.0]],[[389.5,568.4],[396.2,575.1],[383.8,562.4]],[[384.4,554.5],[383.5,561.0],[384.0,545.5]],[[395.4,540.0],[389.5,543.4],[400.1,537.0]]]},
        {name:'right',center:[513.5,538.7],travel:[8,5],
         iris:[[[511.6,515.6],[500.8,515.8],[523.0,516.2]],[[535.3,538.7],[535.0,525.8],[535.9,551.8]],[[514.2,562.4],[526.5,562.2],[502.0,563.0]],[[491.9,539.0],[491.7,552.5],[491.8,526.0]]],
         aperture:[[[512.8,516.2],[502.8,516.5],[522.5,516.5]],[[536.8,529.3],[531.3,519.8],[541.1,534.9]],[[541.2,538.5],[541.5,532.5],[541.2,546.1]],[[514.8,563.5],[530.6,563.3],[500.8,564.0]],[[489.9,542.8],[491.2,555.8],[488.6,535.5]],[[494.6,525.8],[490.3,530.2],[500.2,520.3]]]}
    ];
    var document;
    function exportLayer(doc, layer, filename, trim) {
        var out=app.documents.add(UnitValue(1024,'px'),UnitValue(1024,'px'),72,'Eye rig export',NewDocumentMode.RGB,DocumentFill.TRANSPARENT);
        var blank=out.activeLayer;app.activeDocument=doc;layer.duplicate(out,ElementPlacement.PLACEATBEGINNING);
        app.activeDocument=out;out.activeLayer.visible=true;blank.remove();
        if(trim)out.trim(TrimType.TRANSPARENT,true,true,true,true);
        png(out,File(assets.fsName+'/'+filename));out.close(SaveOptions.DONOTSAVECHANGES);app.activeDocument=doc;
    }
    function preview(doc, name) {
        var all=doc.duplicate('Eye preview '+name,false);png(all,File(previews.fsName+'/'+name+'_full.png'));
        all.crop([UnitValue(370,'px'),UnitValue(475,'px'),UnitValue(645,'px'),UnitValue(640,'px')]);
        all.resizeImage(UnitValue(1100,'px'),UnitValue(660,'px'),72,ResampleMethod.NEARESTNEIGHBOR);
        png(all,File(previews.fsName+'/'+name+'_face.png'));all.close(SaveOptions.DONOTSAVECHANGES);app.activeDocument=doc;
    }
    try {
        record('START actual Photoshop '+app.version);
        document=app.open(File('E:/github/AURA-Desktop-Agent/assets/expressions/neutral.png'));
        var original=document.activeLayer;original.name='Original neutral - unchanged';original.visible=false;
        var base=original.duplicate();base.name='base - clean sclera';base.visible=true;document.activeLayer=base;
        var white=new SolidColor();white.rgb.hexValue='FCFCFD';
        var rigEyes=[];
        for(var i=0;i<eyes.length;i++) {
            var eye=eyes[i];
            eye.apPath=path(document,eye.name+' eye aperture',eye.aperture);
            eye.irisPath=path(document,eye.name+' iris extraction',eye.iris);
            /* Clean sclera is painted only within the inner eyelid aperture.
               Original eyelashes, hair, skin and the whole rest of the art stay intact. */
            document.activeLayer=base;choose(document,eye.apPath);document.selection.fill(white);document.selection.deselect();
            var apertureLayer=document.artLayers.add();apertureLayer.name=eye.name+' aperture - runtime alpha';
            choose(document,eye.apPath);document.selection.fill(white);document.selection.deselect();
            exportLayer(document,apertureLayer,eye.name+'_aperture.png',false);apertureLayer.visible=false;
            document.activeLayer=original;var iris=original.duplicate();iris.name=eye.name+' iris - move this layer';iris.visible=true;
            document.activeLayer=iris;choose(document,eye.irisPath);document.selection.invert();document.selection.clear();document.selection.deselect();
            var b=bounds(iris);record(eye.name+' iris sprite bounds='+b.join(','));
            if(b[2]-b[0]>60 || b[3]-b[1]>60)throw new Error('Iris isolation failed: '+eye.name+' bounds='+b.join(','));
            exportLayer(document,iris,eye.name+'_iris.png',true);
            var group=document.layerSets.add();group.name=eye.name+' eye - fixed eyelid clipping mask';
            iris.move(group,ElementPlacement.INSIDE);document.activeLayer=group;choose(document,eye.apPath);revealSelectionMask();document.selection.deselect();selectRGB();
            eye.layer=iris;
            rigEyes.push({name:eye.name,center:eye.center,iris:eye.name+'_iris.png',iris_origin:[b[0],b[1]],aperture:eye.name+'_aperture.png',travel:eye.travel});
            record(eye.name+' native Photoshop iris isolation and group aperture mask complete');
        }
        exportLayer(document,base,'base.png',false);
        preview(document,'center');
        for(i=0;i<eyes.length;i++)eyes[i].layer.translate(-eyes[i].travel[0],0);
        preview(document,'look_left');
        for(i=0;i<eyes.length;i++)eyes[i].layer.translate(eyes[i].travel[0]*2,0);
        preview(document,'look_right');
        for(i=0;i<eyes.length;i++)eyes[i].layer.translate(-eyes[i].travel[0],-eyes[i].travel[1]);
        preview(document,'look_up');
        for(i=0;i<eyes.length;i++)eyes[i].layer.translate(0,eyes[i].travel[1]*2);
        preview(document,'look_down');
        for(i=0;i<eyes.length;i++)eyes[i].layer.translate(0,-eyes[i].travel[1]);
        original.visible=false;
        var options=new PhotoshopSaveOptions();options.layers=true;options.embedColorProfile=true;
        document.saveAs(File(psds.fsName+'/neutral_eye_rig.psd'),options,false,Extension.LOWERCASE);
        var hashFile=File(work.fsName+'/source_sha256.txt');hashFile.open('r');var hash=hashFile.read().replace(/^\s+|\s+$/g,'');hashFile.close();
        write(File(assets.fsName+'/rig.json'),json({canvas:[1024,1024],source_sha256:hash,base:'base.png',eyes:rigEyes}));
        write(File(work.fsName+'/eye_geometry.json'),json(eyes.slice(0,0).concat([{name:'left',iris:eyes[0].iris,aperture:eyes[0].aperture},{name:'right',iris:eyes[1].iris,aperture:eyes[1].aperture}])));
        record('SUCCESS PSD + base + 2 cropped irises + 2 full canvas apertures + 5 native Photoshop previews');
        return 'EYE_RIG_SUCCESS';
    } catch(e) {record('ERROR line '+e.line+': '+e.message);throw e;}
    finally {app.displayDialogs=oldDialogs;app.preferences.rulerUnits=oldUnits;log.close();}
})();
