#target photoshop
(function(){
var work=File($.fileName).parent,root=work.parent,assets=Folder(root.fsName+'/\u900f\u660e\u5206\u5c42'),previews=Folder(root.fsName+'/\u9884\u89c8');
var oldUnits=app.preferences.rulerUnits,oldDialogs=app.displayDialogs;app.preferences.rulerUnits=Units.PIXELS;app.displayDialogs=DialogModes.NO;
var log=File(work.fsName+'/photoshop_interaction.log');log.encoding='UTF8';log.open('a');
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
function exportLayer(doc,layer,file,trim){var d=app.documents.add(UnitValue(1024,'px'),UnitValue(1024,'px'),72,'Interaction isolated export',NewDocumentMode.RGB,DocumentFill.TRANSPARENT),blank=d.activeLayer;app.activeDocument=doc;layer.duplicate(d,ElementPlacement.PLACEATBEGINNING);app.activeDocument=d;d.activeLayer.visible=true;blank.remove();if(trim)d.trim(TrimType.TRANSPARENT,true,true,true,true);png(d,file);d.close(SaveOptions.DONOTSAVECHANGES);app.activeDocument=doc;}
var parts=[
 {name:'left_ear',pivot:[305,520],amplitude:5,
  shape:[[294.3,487.5],[297.2,498],[300.2,504.5],[305.3,506],[305.1,519],[305,531],[306.1,541],[308.5,550],[313.5,562],[309,568],[300,570],[289,566],[279,570],[268,569],[260,565],[255,562],[243,561],[232,556],[226,552],[223.5,546],[227,539],[238,534],[247,528],[257,520],[267,511],[277,502],[285,494]],
  blue:[[285,500],[305,491],[324,499],[330,535],[321,550],[301,542],[286,529]],
  white:[[288,534],[317,524],[330,537],[327,562],[307,565],[291,555]],blueHex:'2A4A91',whiteHex:'BDD0EE'},
 {name:'right_ear',pivot:[609,488],amplitude:5,
  shape:[[647.1,452.8],[659,455],[671,461.5],[683,469],[697,479],[711,486.5],[722,490.3],[732,493.1],[739,497],[741,503],[738,512],[729,519.2],[718,523.2],[706,525.4],[700,527.7],[695,532.3],[686,536.3],[674,537],[668,535.2],[662,537.9],[653,540.3],[644,540.3],[636,538.4],[631,535.7],[625,536.3],[615,533.6],[608,529.2],[604.6,520.2],[604.4,508],[604.5,497],[603.2,488],[607.3,486.8],[610.9,479.5],[613.7,467.5],[615.6,459.5],[626,462.7],[638.5,466.6],[646.3,465.3]],
  blue:[[594,475],[604,456],[628,451],[650,456],[652,488],[630,504],[605,502]],
  white:[[599,488],[631,489],[650,510],[639,531],[609,528],[599,510]],blueHex:'2C4D95',whiteHex:'C3D4EF'},
 {name:'tail',pivot:[681,844],amplitude:4,
  shape:[[685,863],[678,850],[677,840],[683,830],[695,820],[707,810],[729,800],[742,790],[748,780],[751,770],[751,760],[749,750],[745,740],[742,735],[739,730],[735,725],[731,720],[726,715],[720,710],[714,705],[707,700],[699,695],[692,690],[687,685],[684,680],[681,675],[679,668],[688,663],[695,660],[705,667],[717,679],[713,662],[723,644],[737,632],[753,622],[771,613],[790,605],[802,595],[810,609],[814,628],[814,647],[810,665],[803,682],[792,696],[778,708],[769,716],[765,723],[768,740],[769,758],[765,780],[757,801],[745,821],[729,839],[709,854]],
  blue:[[650,648],[685,654],[700,673],[719,694],[748,728],[751,767],[742,794],[720,821],[685,863],[650,850],[640,821],[672,793],[698,782],[703,762],[689,736],[672,716],[651,692]],blueHex:'2B4C93'}
];
function rotateAt(layer,angle,pivot){var b=bbox(layer),cx=(b[0]+b[2])/2,cy=(b[1]+b[3])/2,dx=pivot[0]-cx,dy=pivot[1]-cy,r=angle*Math.PI/180;layer.rotate(angle,AnchorPosition.MIDDLECENTER);layer.translate(dx-(Math.cos(r)*dx-Math.sin(r)*dy),dy-(Math.sin(r)*dx+Math.cos(r)*dy));}
function preview(doc,name,angles){var d=doc.duplicate(name,false);for(var i=0;i<parts.length;i++){var l=d.artLayers.getByName(parts[i].name+' - movable original plus hidden root');if(angles[i])rotateAt(l,angles[i],parts[i].pivot);}png(d,File(previews.fsName+'/'+name+'_transparent.png'));var bg=d.artLayers.add();bg.name='QA background';bg.move(d.layers[d.layers.length-1],ElementPlacement.PLACEAFTER);d.activeLayer=bg;d.selection.selectAll();d.selection.fill(ink('303746'));d.selection.deselect();png(d,File(previews.fsName+'/'+name+'_dark.png'));d.crop([UnitValue(215,'px'),UnitValue(430,'px'),UnitValue(820,'px'),UnitValue(880,'px')]);d.resizeImage(UnitValue(1210,'px'),UnitValue(900,'px'),72,ResampleMethod.NEARESTNEIGHBOR);png(d,File(previews.fsName+'/'+name+'_detail.png'));d.close(SaveOptions.DONOTSAVECHANGES);app.activeDocument=doc;}
var doc;
try{
 record('START actual Photoshop '+app.version+'; split side fins and tail without source modification');
 doc=app.open(File('E:/github/AURA-Desktop-Agent/assets/expressions/neutral.png'));
 var original=doc.activeLayer;original.name='Original neutral - unchanged hidden';original.visible=false;
 var base=original.duplicate();base.name='base - stationary foreground hair and bows';base.visible=true;
 var cutout=doc.artLayers.add();cutout.name='cutout mask - removed moving pixels only';cutout.visible=false;
 var rigParts=[];
 for(var i=0;i<parts.length;i++){
  var part=parts[i];doc.activeLayer=original;var visible=original.duplicate();visible.name=part.name+' visible source pixels';visible.visible=true;doc.activeLayer=visible;rgb();choose(doc,part.shape);doc.selection.invert();doc.selection.clear();doc.selection.deselect();
  var vb=bbox(visible);if(vb[2]-vb[0]>200||vb[3]-vb[1]>300)throw new Error('Part extraction failed '+part.name+' '+vb.join(','));
  exportLayer(doc,visible,File(assets.fsName+'/'+part.name+'_hit.png'),false);
  var backing=doc.artLayers.add();backing.name=part.name+' hidden same-color root';fill(doc,backing,part.blue,part.blueHex);if(part.white)fill(doc,backing,part.white,part.whiteHex);
  visible.move(backing,ElementPlacement.PLACEBEFORE);doc.activeLayer=visible;var movable=visible.merge();movable.name=part.name+' - movable original plus hidden root';movable.move(base,ElementPlacement.PLACEAFTER);
  doc.activeLayer=base;rgb();choose(doc,part.shape);doc.selection.clear();doc.selection.deselect();
  fill(doc,cutout,part.shape,'FFFFFF');cutout.visible=false;
  var b=bbox(movable);exportLayer(doc,movable,File(assets.fsName+'/'+part.name+'.png'),true);
  rigParts.push({name:part.name,image:part.name+'.png',origin:[b[0],b[1]],pivot:part.pivot,hit_mask:part.name+'_hit.png',amplitude_degrees:part.amplitude,z:'behind'});
  record(part.name+' visible='+vb.join(',')+' exported='+b.join(',')+' pivot='+part.pivot.join(','));
 }
 original.visible=false;cutout.visible=false;exportLayer(doc,base,File(assets.fsName+'/base.png'),false);exportLayer(doc,cutout,File(assets.fsName+'/cutout_mask.png'),false);
 preview(doc,'rest_split',[0,0,0]);preview(doc,'positive',[5,5,4]);preview(doc,'negative',[-5,-5,-4]);preview(doc,'ears_positive',[5,5,0]);preview(doc,'tail_positive',[0,0,4]);preview(doc,'tail_negative',[0,0,-4]);
 original.visible=false;cutout.visible=false;doc.activeLayer=base;
 var psd=new PhotoshopSaveOptions();psd.layers=true;psd.embedColorProfile=true;doc.saveAs(File(root.fsName+'/PSD/neutral_interaction_rig.psd'),psd,false,Extension.LOWERCASE);
 write(File(assets.fsName+'/rig.json'),json({canvas:[1024,1024],source_sha256:read(File(work.fsName+'/source_sha256.txt')),base:'base.png',cutout_mask:'cutout_mask.png',parts:rigParts}));
 record('SUCCESS native PSD, base, cutout, 3 moving parts, positive/negative angle QA');return 'INTERACTION_RIG_SUCCESS';
}catch(e){record('ERROR '+e.line+': '+e.message);throw e;}finally{app.preferences.rulerUnits=oldUnits;app.displayDialogs=oldDialogs;log.close();}
})();
