#target photoshop
(function () {
    var root = new Folder('E:/github/AURA-Desktop-Agent/美术资产/鲸鱼娘_透明背景');
    var work = new Folder(root.fsName + '/工程');
    root.create();
    work.create();
    var oldDialogs = app.displayDialogs;
    var oldUnits = app.preferences.rulerUnits;
    app.displayDialogs = DialogModes.NO;
    app.preferences.rulerUnits = Units.PIXELS;
    var log = new File(work.fsName + '/photoshop_log.txt');
    log.encoding = 'UTF8';
    log.open('w');
    function record(s) { log.writeln(s); }
    function wand(x, y, add) {
        var d = new ActionDescriptor();
        var r = new ActionReference();
        r.putProperty(charIDToTypeID('Chnl'), charIDToTypeID('fsel'));
        d.putReference(charIDToTypeID('null'), r);
        var p = new ActionDescriptor();
        p.putUnitDouble(charIDToTypeID('Hrzn'), charIDToTypeID('#Pxl'), x);
        p.putUnitDouble(charIDToTypeID('Vrtc'), charIDToTypeID('#Pxl'), y);
        d.putObject(charIDToTypeID('T   '), charIDToTypeID('Pnt '), p);
        d.putInteger(charIDToTypeID('Tlrn'), 32);
        d.putBoolean(charIDToTypeID('AntA'), true);
        d.putBoolean(charIDToTypeID('Cntg'), true);
        d.putBoolean(charIDToTypeID('Mrgd'), false);
        executeAction(charIDToTypeID(add ? 'AddT' : 'setd'), d, DialogModes.NO);
    }
    function savePng(doc, file) {
        var options = new PNGSaveOptions();
        options.interlaced = false;
        options.compression = 9;
        doc.saveAs(file, options, true, Extension.LOWERCASE);
    }
    function preview(doc, color, filename) {
        var copy = doc.duplicate('Edge inspection', true);
        var bg = copy.artLayers.add();
        bg.name = 'Inspection background';
        bg.move(copy.layers[copy.layers.length - 1], ElementPlacement.PLACEAFTER);
        var ink = new SolidColor();
        ink.rgb.hexValue = color;
        copy.selection.selectAll();
        copy.selection.fill(ink);
        copy.selection.deselect();
        savePng(copy, new File(work.fsName + '/' + filename));
        copy.close(SaveOptions.DONOTSAVECHANGES);
    }
    try {
        record('Photoshop ' + app.version);
        var source = new File('E:/github/AURA-Desktop-Agent/美术资产/7cb43b514fde4132299895825136e63.jpg');
        var doc = app.open(source);
        var original = doc.activeLayer;
        original.name = '原图（保留）';
        var cutout = original.duplicate();
        cutout.name = '鲸鱼娘 · 透明背景';
        original.visible = false;
        doc.activeLayer = cutout;
        wand(5, 5, false);
        wand(185, 708, true);
        wand(971, 645, true);
        wand(106, 895, true);
        doc.selection.expand(1);
        doc.selection.clear();
        doc.selection.deselect();
        record('Removed outer background and three enclosed hair gaps.');
        var defringe = new ActionDescriptor();
        defringe.putInteger(charIDToTypeID('Wdth'), 1);
        executeAction(charIDToTypeID('Dfrg'), defringe, DialogModes.NO);
        record('Defringed edge by one pixel.');
        savePng(doc, new File(root.fsName + '/鲸鱼娘_透明背景.png'));
        var psd = new PhotoshopSaveOptions();
        psd.layers = true;
        psd.embedColorProfile = true;
        doc.saveAs(new File(root.fsName + '/鲸鱼娘_可编辑.psd'), psd, false, Extension.LOWERCASE);
        preview(doc, '252B39', 'preview_dark.png');
        preview(doc, 'D794AC', 'preview_pink.png');
        app.activeDocument = doc;
        record('SUCCESS: 1259 x 1259 transparent PNG and layered PSD.');
        return 'SUCCESS';
    } catch (e) {
        record('ERROR line ' + e.line + ': ' + e.message);
        throw e;
    } finally {
        app.preferences.rulerUnits = oldUnits;
        app.displayDialogs = oldDialogs;
        log.close();
    }
})();
