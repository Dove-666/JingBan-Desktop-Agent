#target photoshop
(function(){var root=File($.fileName).parent.parent;var d=app.open(File(root.fsName+'/PSD/talk_open_interaction_rig.psd'));d.activeLayer=d.artLayers.getByName('base - stationary foreground hair and bows');try{app.runMenuItem(charIDToTypeID('FtOn'));}catch(e){}app.refresh();return 'Native '+d.name+' layers='+d.layers.length+' saved='+d.saved;})();
