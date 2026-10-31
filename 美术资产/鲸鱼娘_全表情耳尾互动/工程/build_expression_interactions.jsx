#target photoshop
(function(){
var work=File($.fileName).parent,root=work.parent,oldUnits=app.preferences.rulerUnits,oldDialogs=app.displayDialogs;
app.preferences.rulerUnits=Units.PIXELS;app.displayDialogs=DialogModes.NO;
var log=File(work.fsName+'/photoshop_batch.log');log.encoding='UTF8';log.open('a');
function record(s){log.writeln(new Date()+' '+s);log.close();log.open('a');}
function c(s){return charIDToTypeID(s);}
function rgb(){var d=new ActionDescriptor(),r=new ActionReference();r.putEnumerated(c('Chnl'),c('Chnl'),c('RGB '));d.putReference(c('null'),r);executeAction(c('slct'),d,DialogModes.NO);}
function ink(hex){var s=new SolidColor();s.rgb.hexValue=hex;return s;}
function choose(doc,p){app.activeDocument=doc;doc.selection.select(p,SelectionType.REPLACE,0,true);}
function fill(doc,layer,p,color){doc.activeLayer=layer;rgb();choose(doc,p);doc.selection.fill(ink(color));doc.selection.deselect();}
function bbox(layer){var b=layer.bounds;return [b[0].as('px'),b[1].as('px'),b[2].as('px'),b[3].as('px')];}
function png(doc,file){var o=new PNGSaveOptions();o.compression=9;doc.saveAs(file,o,true,Extension.LOWERCASE);}
function write(file,value){file.encoding='UTF8';file.open('w');file.write(value);file.close();}
function read(file){file.encoding='UTF8';file.open('r');var s=file.read().replace(/^\uFEFF/,'').replace(/^\s+|\s+$/g,'');file.close();return s;}
function json(v){if(v===null)return 'null';if(typeof v==='number'||typeof v==='boolean')return String(v);if(typeof v==='string')return '"'+v.replace(/\\/g,'\\\\').replace(/"/g,'\\"')+'"';var p=[],i;if(v instanceof Array){for(i=0;i<v.length;i++)p.push(json(v[i]));return '['+p.join(',')+']';}for(i in v)if(v.hasOwnProperty(i))p.push(json(i)+':'+json(v[i]));return '{'+p.join(',')+'}';}
function exportLayer(doc,layer,file,trim){var d=app.documents.add(UnitValue(1024,'px'),UnitValue(1024,'px'),72,'Expression interaction export',NewDocumentMode.RGB,DocumentFill.TRANSPARENT),blank=d.activeLayer;app.activeDocument=doc;layer.duplicate(d,ElementPlacement.PLACEATBEGINNING);app.activeDocument=d;d.activeLayer.visible=true;blank.remove();if(trim)d.trim(TrimType.TRANSPARENT,true,true,true,true);png(d,file);d.close(SaveOptions.DONOTSAVECHANGES);app.activeDocument=doc;}
function rotateAt(layer,angle,pivot){var b=bbox(layer),cx=(b[0]+b[2])/2,cy=(b[1]+b[3])/2,dx=pivot[0]-cx,dy=pivot[1]-cy,r=angle*Math.PI/180;layer.rotate(angle,AnchorPosition.MIDDLECENTER);layer.translate(dx-(Math.cos(r)*dx-Math.sin(r)*dy),dy-(Math.sin(r)*dx+Math.cos(r)*dy));}
function preview(doc,parts,dir,name,angles){var d=doc.duplicate(name,false);for(var i=0;i<parts.length;i++){var l=d.artLayers.getByName(parts[i].name+' - movable original plus hidden root');if(angles[i])rotateAt(l,angles[i],parts[i].pivot);}png(d,File(dir.fsName+'/'+name+'_transparent.png'));var bg=d.artLayers.add();bg.name='QA background';bg.move(d.layers[d.layers.length-1],ElementPlacement.PLACEAFTER);d.activeLayer=bg;d.selection.selectAll();d.selection.fill(ink('303746'));d.selection.deselect();png(d,File(dir.fsName+'/'+name+'_dark.png'));d.activeLayer=bg;d.selection.selectAll();d.selection.fill(ink('F0E9E2'));d.selection.deselect();png(d,File(dir.fsName+'/'+name+'_light.png'));d.close(SaveOptions.DONOTSAVECHANGES);app.activeDocument=doc;}
function sourceDetail(doc,dir,name,bounds,scale){var d=doc.duplicate(name,false);d.activeLayer.visible=true;d.crop([UnitValue(bounds[0],'px'),UnitValue(bounds[1],'px'),UnitValue(bounds[2],'px'),UnitValue(bounds[3],'px')]);d.resizeImage(UnitValue((bounds[2]-bounds[0])*scale,'px'),UnitValue((bounds[3]-bounds[1])*scale,'px'),72,ResampleMethod.NEARESTNEIGHBOR);png(d,File(dir.fsName+'/'+name+'.png'));d.close(SaveOptions.DONOTSAVECHANGES);app.activeDocument=doc;}
var geometry=eval('('+read(File(work.fsName+'/geometry.json'))+')'),hashes=eval('('+read(File(work.fsName+'/source_hashes.json'))+')');
function process(state){
 var parts=geometry[state],assets=Folder(root.fsName+'/\u900f\u660e\u5206\u5c42/'+state),previews=Folder(root.fsName+'/\u9884\u89c8/'+state);assets.create();previews.create();
 var doc=app.open(File('E:/github/AURA-Desktop-Agent/assets/expressions/'+state+'.png'));
 sourceDetail(doc,previews,'source_ears_3x',[220,450,742,575],3);sourceDetail(doc,previews,'source_tail_3x',[635,590,820,875],3);
 var original=doc.activeLayer;original.name='Original '+state+' - unchanged hidden';original.visible=false;
 var base=original.duplicate();base.name='base - stationary foreground hair and bows';base.visible=true;
 var cutout=doc.artLayers.add();cutout.name='cutout mask - removed moving pixels only';cutout.visible=false;
 var rigParts=[];
 for(var i=0;i<parts.length;i++){
  var part=parts[i];doc.activeLayer=original;var visible=original.duplicate();visible.name=part.name+' visible source pixels';visible.visible=true;doc.activeLayer=visible;rgb();choose(doc,part.shape);doc.selection.invert();doc.selection.clear();doc.selection.deselect();
  var vb=bbox(visible);if(vb[2]-vb[0]>200||vb[3]-vb[1]>300)throw new Error('Part extraction failed '+state+' '+part.name+' '+vb.join(','));
  exportLayer(doc,visible,File(assets.fsName+'/'+part.name+'_hit.png'),false);
  var backing=doc.artLayers.add();backing.name=part.name+' hidden same-color root';fill(doc,backing,part.blue,part.blueHex);if(part.white)fill(doc,backing,part.white,part.whiteHex);
  visible.move(backing,ElementPlacement.PLACEBEFORE);doc.activeLayer=visible;var movable=visible.merge();movable.name=part.name+' - movable original plus hidden root';movable.move(base,ElementPlacement.PLACEAFTER);
  var removal=part.clear_shape||part.shape;doc.activeLayer=base;rgb();choose(doc,removal);doc.selection.clear();doc.selection.deselect();fill(doc,cutout,removal,'FFFFFF');cutout.visible=false;
  var b=bbox(movable);exportLayer(doc,movable,File(assets.fsName+'/'+part.name+'.png'),true);
  rigParts.push({name:part.name,image:part.name+'.png',origin:[b[0],b[1]],pivot:part.pivot,hit_mask:part.name+'_hit.png',amplitude_degrees:part.amplitude,z:'behind'});
  record(state+' '+part.name+' native source='+vb.join(',')+' export='+b.join(',')+' pivot='+part.pivot.join(','));
 }
 original.visible=false;cutout.visible=false;exportLayer(doc,base,File(assets.fsName+'/base.png'),false);exportLayer(doc,cutout,File(assets.fsName+'/cutout_mask.png'),false);
 preview(doc,parts,previews,'rest_split',[0,0,0]);preview(doc,parts,previews,'positive',[5,5,4]);preview(doc,parts,previews,'negative',[-5,-5,-4]);
 original.visible=false;cutout.visible=false;doc.activeLayer=base;
 var psd=new PhotoshopSaveOptions();psd.layers=true;psd.embedColorProfile=true;doc.saveAs(File(root.fsName+'/PSD/'+state+'_interaction_rig.psd'),psd,false,Extension.LOWERCASE);
 write(File(assets.fsName+'/rig.json'),json({canvas:[1024,1024],source_sha256:hashes[state],base:'base.png',cutout_mask:'cutout_mask.png',parts:rigParts}));
 record('SUCCESS '+state+' native PSD + 9 assets + rest/positive/negative in dark/light backgrounds');doc.close(SaveOptions.DONOTSAVECHANGES);
}
try{record('START actual Photoshop '+app.version);var states=read(File(work.fsName+'/batch_states.txt')).split(/[\r\n,]+/);for(var i=0;i<states.length;i++){var s=states[i].replace(/^\s+|\s+$/g,'');if(s)process(s);}record('COMPLETE');return 'ALL_EXPRESSION_INTERACTIONS_SUCCESS';}catch(e){record('ERROR '+e.line+': '+e.message);throw e;}finally{app.preferences.rulerUnits=oldUnits;app.displayDialogs=oldDialogs;log.close();}
})();
