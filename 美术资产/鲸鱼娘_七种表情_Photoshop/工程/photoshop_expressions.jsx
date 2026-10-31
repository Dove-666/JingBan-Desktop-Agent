#target photoshop
/* Run inside Adobe Photoshop. All cutout, masking, defringing and sizing are
   performed by Photoshop. Raw inputs remain untouched. */
(function () {
    var work = File($.fileName).parent;
    var root = work.parent;
    var raw = Folder(root.fsName + '/\u539f\u59cb\u751f\u56fe');
    var psds = Folder(root.fsName + '/PSD');
    var pngs = Folder(root.fsName + '/\u900f\u660ePNG');
    var checks = Folder(root.fsName + '/\u68c0\u67e5\u9884\u89c8');
    psds.create(); pngs.create(); checks.create();
    var oldDialogs = app.displayDialogs;
    var oldUnits = app.preferences.rulerUnits;
    app.displayDialogs = DialogModes.NO;
    app.preferences.rulerUnits = Units.PIXELS;
    var log = File(work.fsName + '/photoshop_batch.log');
    log.encoding = 'UTF8'; log.open('a');
    function record(s) { log.writeln(new Date() + ' ' + s); log.close(); log.open('a'); }
    function sID(v) { return stringIDToTypeID(v); }
    function cID(v) { return charIDToTypeID(v); }
    function png(doc, file) {
        var opt = new PNGSaveOptions(); opt.interlaced = false; opt.compression = 9;
        doc.saveAs(file, opt, true, Extension.LOWERCASE);
    }
    function bounds(layer) {
        var b = layer.bounds;
        return [b[0].as('px'), b[1].as('px'), b[2].as('px'), b[3].as('px')];
    }
    function selectChannel(name) {
        var desc = new ActionDescriptor(); var ref = new ActionReference();
        ref.putEnumerated(cID('Chnl'), cID('Chnl'), cID(name)); desc.putReference(cID('null'), ref);
        executeAction(cID('slct'), desc, DialogModes.NO);
    }
    function alphaToMask() {
        var desc = new ActionDescriptor();
        desc.putClass(cID('Nw  '), cID('Chnl'));
        var at = new ActionReference();
        at.putEnumerated(cID('Chnl'), cID('Chnl'), cID('Msk '));
        desc.putReference(cID('At  '), at);
        desc.putEnumerated(cID('Usng'), cID('UsrM'), cID('Trns'));
        executeAction(cID('Mk  '), desc, DialogModes.NO);
    }
    function cleanMaskLevels() {
        var desc = new ActionDescriptor();
        desc.putEnumerated(sID('presetKind'), sID('presetKindType'), sID('presetKindCustom'));
        var adjustments = new ActionList(); var level = new ActionDescriptor();
        var channel = new ActionReference(); channel.putEnumerated(cID('Chnl'), cID('Chnl'), cID('Msk '));
        level.putReference(cID('Chnl'), channel);
        var input = new ActionList(); input.putInteger(8); input.putInteger(250); level.putList(cID('Inpt'), input);
        level.putDouble(cID('Gmm '), 1);
        var output = new ActionList(); output.putInteger(0); output.putInteger(255); level.putList(cID('Otpt'), output);
        adjustments.putObject(cID('LvlA'), level); desc.putList(cID('Adjs'), adjustments);
        executeAction(cID('Lvls'), desc, DialogModes.NO);
    }
    function preview(doc, hex, file) {
        var copy = doc.duplicate(doc.name + '_inspection', false);
        var ink = new SolidColor(); ink.rgb.hexValue = hex;
        var bg = copy.artLayers.add(); bg.name = 'Inspection only';
        bg.move(copy.layers[copy.layers.length - 1], ElementPlacement.PLACEAFTER);
        copy.activeLayer = bg; copy.selection.selectAll(); copy.selection.fill(ink); copy.selection.deselect();
        png(copy, file); copy.close(SaveOptions.DONOTSAVECHANGES);
    }
    function cutout(file, state) {
        record(state + ' source=' + file.fsName + '; exists=' + file.exists);
        var input = app.open(file);
        input.activeLayer.name = 'Raw original - unmodified';
        var out = app.documents.add(UnitValue(1024, 'px'), UnitValue(1024, 'px'), 72,
            state + '_Photoshop', NewDocumentMode.RGB, DocumentFill.TRANSPARENT);
        var placeholder = out.activeLayer;
        app.activeDocument = input;
        input.activeLayer.duplicate(out, ElementPlacement.PLACEATBEGINNING);
        app.activeDocument = out;
        var original = out.activeLayer; original.visible = false;
        var layer = original.duplicate(); layer.name = state + ' - editable Photoshop cutout'; layer.visible = true;
        out.activeLayer = layer; placeholder.remove();
        input.close(SaveOptions.DONOTSAVECHANGES);
        var initial = bounds(layer);
        record(state + ' source bounds=' + initial.join(','));
        /* Defringe replaces white RGB contamination at the existing alpha edge.
           It runs before downscaling to keep hair and the white dress intact. */
        var defringe = new ActionDescriptor(); defringe.putInteger(cID('Wdth'), 1);
        executeAction(cID('Dfrg'), defringe, DialogModes.NO);
        record(state + ' Photoshop Defringe 1px applied');
        /* Native Layer > Layer Mask > From Transparency transfers the PNG alpha
           to an editable Photoshop layer mask without squaring soft edge alpha. */
        alphaToMask();
        selectChannel('Msk ');
        cleanMaskLevels();
        record(state + ' Photoshop mask Levels input black=8 white=250 gamma=1');
        selectChannel('RGB ');
        var before = bounds(layer);
        var height = before[3] - before[1];
        if (height < 64) throw new Error('Empty or invalid subject: ' + state);
        var scale = 66600 / height;
        layer.resize(scale, scale, AnchorPosition.MIDDLECENTER);
        var after = bounds(layer);
        layer.translate(512 - (after[0] + after[2]) / 2, 960 - after[3]);
        record(state + ' normalized subject bounds=' + bounds(layer).join(',') + '; scale=' + scale);
        var psd = new PhotoshopSaveOptions(); psd.layers = true; psd.embedColorProfile = true; psd.alphaChannels = true;
        out.saveAs(File(psds.fsName + '/' + state + '.psd'), psd, false, Extension.LOWERCASE);
        png(out, File(pngs.fsName + '/' + state + '.png'));
        preview(out, '252B39', File(checks.fsName + '/' + state + '_dark.png'));
        preview(out, 'D794AC', File(checks.fsName + '/' + state + '_pink.png'));
        if (state === 'neutral') {
            var alias = out.duplicate('talk_closed - source neutral', false);
            alias.saveAs(File(psds.fsName + '/talk_closed.psd'), psd, false, Extension.LOWERCASE);
            png(alias, File(pngs.fsName + '/talk_closed.png'));
            alias.close(SaveOptions.DONOTSAVECHANGES);
            record('ALIAS talk_closed exported from neutral; identical closed mouth source');
        }
        record('SUCCESS ' + state + ': PSD + transparent PNG + two inspection PNGs');
        out.close(SaveOptions.DONOTSAVECHANGES);
    }
    try {
        record('START Photoshop ' + app.version + '; root=' + root.fsName);
        var request = File(work.fsName + '/batch_states.txt');
        var states = ['neutral','happy','surprised','sleepy','angry','thinking','talk_open'];
        if (request.exists) {
            request.encoding = 'UTF8'; request.open('r'); states = request.read().replace(/^\uFEFF/, '').split(/[\r\n,]+/); request.close();
        }
        var count = 0;
        for (var i = 0; i < states.length; i++) {
            var state = states[i].replace(/^\s+|\s+$/g, ''); if (!state) continue;
            if (!/^[a-z_]+$/.test(state)) throw new Error('Invalid state name: ' + state);
            var file = File(raw.fsName + '/' + state + '.png');
            if (!file.exists) throw new Error('Missing raw input: ' + file.fsName);
            cutout(file, state); count++;
        }
        record('COMPLETE count=' + count);
        var done = File(work.fsName + '/photoshop_success.txt'); done.open('w'); done.write('SUCCESS count=' + count + '\nPhotoshop ' + app.version); done.close();
        return 'SUCCESS count=' + count;
    } catch (e) {
        record('ERROR line ' + e.line + ': ' + e.message);
        var error = File(work.fsName + '/photoshop_error.txt'); error.encoding = 'UTF8'; error.open('w'); error.write('line ' + e.line + ': ' + e.message); error.close();
        throw e;
    } finally {
        app.preferences.rulerUnits = oldUnits; app.displayDialogs = oldDialogs; log.close();
    }
})();
