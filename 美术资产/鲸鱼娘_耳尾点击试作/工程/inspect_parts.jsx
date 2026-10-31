#target photoshop
(function(){
 var root=File($.fileName).parent.parent, out=Folder(root.fsName+'/\u9884\u89c8');
 var old=app.preferences.rulerUnits,dialogs=app.displayDialogs;
 app.preferences.rulerUnits=Units.PIXELS;app.displayDialogs=DialogModes.NO;
 var src=app.open(File('E:/github/AURA-Desktop-Agent/assets/expressions/neutral.png'));
 function crop(name,b,scale){var d=src.duplicate(name,false);d.crop([UnitValue(b[0],'px'),UnitValue(b[1],'px'),UnitValue(b[2],'px'),UnitValue(b[3],'px')]);d.resizeImage(UnitValue((b[2]-b[0])*scale,'px'),UnitValue((b[3]-b[1])*scale,'px'),72,ResampleMethod.NEARESTNEIGHBOR);var o=new PNGSaveOptions();o.compression=9;d.saveAs(File(out.fsName+'/'+name+'.png'),o,true,Extension.LOWERCASE);d.close(SaveOptions.DONOTSAVECHANGES);}
 try{crop('left_ear_source_6x',[210,430,365,620],6);crop('right_ear_source_6x',[565,410,765,580],6);crop('tail_source_4x',[600,570,830,910],4);}finally{src.close(SaveOptions.DONOTSAVECHANGES);app.preferences.rulerUnits=old;app.displayDialogs=dialogs;}
 return 'Native Photoshop source detail crops saved';
})();
