#target photoshop
(function(){
var work=File($.fileName).parent,root=work.parent;
$.evalFile(File(work.fsName+'/geometry.jsx'));
var log=File(work.fsName+'/photoshop_expression_eyes.log');log.encoding='UTF8';log.open('a');
function record(s){log.writeln(new Date()+' '+s);log.close();log.open('a');}
function c(s){return charIDToTypeID(s);}
function png(doc,file){var o=new PNGSaveOptions();o.compression=9;doc.saveAs(file,o,true,Extension.LOWERCASE);}
function read(file){file.encoding='UTF8';file.open('r');var v=file.read().replace(/^\uFEFF/,'');file.close();return v;}
function write(file,value){file.encoding='UTF8';file.open('w');file.write(value);file.close();}
function json(v){if(v===null)return 'null';if(typeof v==='number'||typeof v==='boolean')return String(v);if(typeof v==='string')return '"'+v.replace(/\\/g,'\\\\').replace(/"/g,'\\"').replace(/\r/g,'\\r').replace(/\n/g,'\\n')+'"';var p=[],i;if(v instanceof Array){for(i=0;i<v.length;i++)p.push(json(v[i]));return '['+p.join(',')+']';}for(i in v)if(v.hasOwnProperty(i))p.push(json(i)+':'+json(v[i]));return '{'+p.join(',')+'}';}
function polygon(knots){var points=[];for(var i=0;i<knots.length;i++){var a=knots[i],b=knots[(i+1)%knots.length];for(var j=0;j<20;j++){var t=j/20,u=1-t;points.push([u*u*u*a[0][0]+3*u*u*t*a[2][0]+3*u*t*t*b[1][0]+t*t*t*b[0][0],u*u*u*a[0][1]+3*u*u*t*a[2][1]+3*u*t*t*b[1][1]+t*t*t*b[0][1]]);}}return points;}
function choose(doc,points,type){app.activeDocument=doc;doc.selection.select(points,type||SelectionType.REPLACE,0,true);}
function rgb(){var d=new ActionDescriptor(),r=new ActionReference();r.putEnumerated(c('Chnl'),c('Chnl'),c('RGB '));d.putReference(c('null'),r);executeAction(c('slct'),d,DialogModes.NO);}
function mask(){var d=new ActionDescriptor();d.putClass(c('Nw  '),c('Chnl'));var r=new ActionReference();r.putEnumerated(c('Chnl'),c('Chnl'),c('Msk '));d.putReference(c('At  '),r);d.putEnumerated(c('Usng'),c('UsrM'),c('RvlS'));executeAction(c('Mk  '),d,DialogModes.NO);}
function bounds(layer){var b=layer.bounds;return [b[0].as('px'),b[1].as('px'),b[2].as('px'),b[3].as('px')];}
function ink(hex){var s=new SolidColor();s.rgb.hexValue=hex;return s;}
var oldDialogs=app.displayDialogs,oldUnits=app.preferences.rulerUnits;app.displayDialogs=DialogModes.NO;app.preferences.rulerUnits=Units.PIXELS;
function exportLayer(doc,layer,file,trim){var out=app.documents.add(UnitValue(1024,'px'),UnitValue(1024,'px'),72,'Isolated eye asset',NewDocumentMode.RGB,DocumentFill.TRANSPARENT);var blank=out.activeLayer;app.activeDocument=doc;layer.duplicate(out,ElementPlacement.PLACEATBEGINNING);app.activeDocument=out;out.activeLayer.visible=true;blank.remove();if(trim)out.trim(TrimType.TRANSPARENT,true,true,true,true);png(out,file);out.close(SaveOptions.DONOTSAVECHANGES);app.activeDocument=doc;}
function preview(doc,dir,name){var p=doc.duplicate(name,false);png(p,File(dir.fsName+'/'+name+'_full.png'));p.crop([UnitValue(370,'px'),UnitValue(465,'px'),UnitValue(650,'px'),UnitValue(645,'px')]);p.resizeImage(UnitValue(1120,'px'),UnitValue(720,'px'),72,ResampleMethod.NEARESTNEIGHBOR);png(p,File(dir.fsName+'/'+name+'_face.png'));p.close(SaveOptions.DONOTSAVECHANGES);app.activeDocument=doc;}
function process(state){
 var config=expressionEyeConfigs[state];if(!config)throw new Error('No independent geometry for '+state);
 var assets=Folder(root.fsName+'/\u900f\u660e\u5206\u5c42/'+state),previews=Folder(root.fsName+'/\u9884\u89c8/'+state);assets.create();previews.create();
 var doc=app.open(File('E:/github/AURA-Desktop-Agent/assets/expressions/'+state+'.png'));var original=doc.activeLayer;original.name='Original '+state+' - unchanged';original.visible=false;
 var base=original.duplicate();base.name='base - clean sclera';base.visible=true;doc.activeLayer=base;var rigEyes=[],layers=[];
 for(var i=0;i<config.eyes.length;i++){
  var eye=config.eyes[i],ap=polygon(eye.aperture),irisPoints=polygon(eye.iris);
  doc.activeLayer=base;choose(doc,ap);doc.selection.fill(ink('FCFCFD'));doc.selection.deselect();
  var aperture=doc.artLayers.add();aperture.name=eye.name+' aperture - runtime alpha';choose(doc,ap);doc.selection.fill(ink('FFFFFF'));doc.selection.deselect();exportLayer(doc,aperture,File(assets.fsName+'/'+eye.name+'_aperture.png'),false);aperture.visible=false;
  doc.activeLayer=original;var iris=original.duplicate();iris.name=eye.name+' iris - move this layer';iris.visible=true;doc.activeLayer=iris;rgb();choose(doc,irisPoints);choose(doc,ap,SelectionType.INTERSECT);if(config.reconstruct)doc.selection.contract(1);doc.selection.invert();doc.selection.clear();doc.selection.deselect();
  if(config.reconstruct){
   var sourceFringe=new ActionDescriptor();sourceFringe.putInteger(c('Wdth'),1);executeAction(c('Dfrg'),sourceFringe,DialogModes.NO);
   var cap=doc.artLayers.add();cap.name=eye.name+' hidden upper iris cap - native Photoshop';choose(doc,irisPoints);doc.selection.fill(ink('101D38'));doc.selection.deselect();
   var inner=[];for(var j=0;j<irisPoints.length;j++)inner.push([eye.center[0]+(irisPoints[j][0]-eye.center[0])*0.955,eye.center[1]+(irisPoints[j][1]-eye.center[1])*0.960]);choose(doc,inner);doc.selection.fill(ink('153875'));doc.selection.deselect();
   iris.move(cap,ElementPlacement.PLACEBEFORE);doc.activeLayer=iris;iris=iris.merge();iris.name=eye.name+' iris - original visible art plus hidden cap';
  }
  doc.activeLayer=iris;rgb();var fringe=new ActionDescriptor();fringe.putInteger(c('Wdth'),1);executeAction(c('Dfrg'),fringe,DialogModes.NO);
  var b=bounds(iris);if(b[2]-b[0]>100||b[3]-b[1]>100||b[2]-b[0]<10)throw new Error('Iris bounds invalid '+state+' '+eye.name+' '+b.join(','));record(state+' '+eye.name+' isolated iris='+b.join(',')+'; Photoshop Defringe1'+(config.reconstruct?'; visible original inset1px, hidden cap preserved':''));exportLayer(doc,iris,File(assets.fsName+'/'+eye.name+'_iris.png'),true);
  var group=doc.layerSets.add();group.name=eye.name+' fixed expression eyelid mask';iris.move(group,ElementPlacement.INSIDE);doc.activeLayer=group;choose(doc,ap);mask();doc.selection.deselect();rgb();layers.push(iris);
  rigEyes.push({name:eye.name,center:eye.center,iris:eye.name+'_iris.png',iris_origin:[b[0],b[1]],aperture:eye.name+'_aperture.png',travel:eye.travel});
 }
 original.visible=false;exportLayer(doc,base,File(assets.fsName+'/base.png'),false);
 var views=[['center',0,0],['look_left',-1,0],['look_right',1,0],['look_up',0,-1],['look_down',0,1],['look_up_left',-1,-1],['look_up_right',1,-1],['look_down_left',-1,1],['look_down_right',1,1]],last=[0,0];
 for(var k=0;k<views.length;k++){var v=views[k];for(i=0;i<layers.length;i++)layers[i].translate((v[1]-last[0])*config.eyes[i].travel[0],(v[2]-last[1])*config.eyes[i].travel[1]);preview(doc,previews,v[0]);last=[v[1],v[2]];}
 for(i=0;i<layers.length;i++)layers[i].translate(-last[0]*config.eyes[i].travel[0],-last[1]*config.eyes[i].travel[1]);original.visible=false;
 var psd=new PhotoshopSaveOptions();psd.layers=true;psd.embedColorProfile=true;doc.saveAs(File(root.fsName+'/PSD/'+state+'_eye_rig.psd'),psd,false,Extension.LOWERCASE);
 var hash=read(File(work.fsName+'/'+state+'_source_sha256.txt')).replace(/^\s+|\s+$/g,'');write(File(assets.fsName+'/rig.json'),json({canvas:[1024,1024],source_sha256:hash,base:'base.png',eyes:rigEyes}));record('SUCCESS '+state+' native PS rig, independent expression apertures, 9 directions');doc.close(SaveOptions.DONOTSAVECHANGES);
}
try{var states=read(File(work.fsName+'/batch_states.txt')).split(/[\r\n,]+/);record('START Photoshop '+app.version);for(var n=0;n<states.length;n++){var state=states[n].replace(/^\s+|\s+$/g,'');if(state)process(state);}record('COMPLETE');return 'EXPRESSION_EYES_SUCCESS';}catch(e){record('ERROR line '+e.line+': '+e.message);throw e;}finally{app.displayDialogs=oldDialogs;app.preferences.rulerUnits=oldUnits;log.close();}
})();
