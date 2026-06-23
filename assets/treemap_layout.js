(function(){var SECTOR_INSET_PCT=0.4;var LOGO_MIN_W=80;var LOGO_MIN_H=50;var TEXT_MIN_W=60;var TEXT_MIN_H=46;var FOLD_SAFETY_PX=1;function fmtPct(value){var abs=Math.abs(value);var rounded=Math.round(abs*10)/10;if(rounded>=100){return String(Math.round(value));}
return value.toFixed(1);}
function stripExchange(ticker){var idx=ticker.indexOf(":");if(idx>=0)return ticker.slice(idx+1);return ticker;}
function rowWorst(values,length){if(!values.length)return Infinity;var rowSum=0;var rMax=values[0];var rMin=values[0];for(var i=0;i<values.length;i++){rowSum+=values[i];if(values[i]>rMax)rMax=values[i];if(values[i]<rMin)rMin=values[i];}
if(rowSum<=0||rMin<=0)return Infinity;var s2=rowSum*rowSum;var l2=length*length;return Math.max((l2*rMax)/s2,s2/(l2*rMin));}
function squarify(values,rect){var placed=new Array(values.length);var n=values.length;if(!n)return[];var total=0;for(var ti=0;ti<n;ti++)total+=values[ti];if(total<=0||rect.w<=0||rect.h<=0){for(var zi=0;zi<n;zi++){placed[zi]={x:rect.x,y:rect.y,w:0,h:0};}
return placed;}
var area=rect.w*rect.h;var scaled=new Array(n);for(var si=0;si<n;si++){scaled[si]=values[si]*area/total;}
var remainingIdx=[];for(var ri=0;ri<n;ri++)remainingIdx.push(ri);var remainingRect={x:rect.x,y:rect.y,w:rect.w,h:rect.h};while(remainingIdx.length){var rowIdx=[remainingIdx[0]];if(remainingRect.w<=0||remainingRect.h<=0){for(var ei=0;ei<remainingIdx.length;ei++){placed[remainingIdx[ei]]={x:remainingRect.x,y:remainingRect.y,w:0,h:0,};}
break;}
var short=Math.min(remainingRect.w,remainingRect.h);var rowVals=[scaled[rowIdx[0]]];var curWorst=rowWorst(rowVals,short);var i=1;while(i<remainingIdx.length){var candidate=rowIdx.concat([remainingIdx[i]]);var candVals=[];for(var ci=0;ci<candidate.length;ci++){candVals.push(scaled[candidate[ci]]);}
var candWorst=rowWorst(candVals,short);if(candWorst>curWorst)break;rowIdx=candidate;rowVals=candVals;curWorst=candWorst;i+=1;}
var rowSum=0;for(var rs=0;rs<rowVals.length;rs++)rowSum+=rowVals[rs];if(remainingRect.w>=remainingRect.h){var rowW=rowSum/remainingRect.h;var ry=remainingRect.y;for(var lj=0;lj<rowIdx.length;lj++){var v=rowVals[lj];var rh=rowW>0?v/rowW:0;placed[rowIdx[lj]]={x:remainingRect.x,y:ry,w:rowW,h:rh,};ry+=rh;}
remainingRect={x:remainingRect.x+rowW,y:remainingRect.y,w:remainingRect.w-rowW,h:remainingRect.h,};}else{var rowH=rowSum/remainingRect.w;var rx=remainingRect.x;for(var mj=0;mj<rowIdx.length;mj++){var mv=rowVals[mj];var rw=rowH>0?mv/rowH:0;placed[rowIdx[mj]]={x:rx,y:remainingRect.y,w:rw,h:rowH,};rx+=rw;}
remainingRect={x:remainingRect.x,y:remainingRect.y+rowH,w:remainingRect.w,h:remainingRect.h-rowH,};}
remainingIdx=remainingIdx.slice(i);}
for(var pi=0;pi<n;pi++){if(!placed[pi])placed[pi]={x:0,y:0,w:0,h:0};}
return placed;}
function insetRect(rect,pad){if(rect.w<=2*pad||rect.h<=2*pad){return{x:rect.x,y:rect.y,w:0,h:0};}
return{x:rect.x+pad,y:rect.y+pad,w:rect.w-2*pad,h:rect.h-2*pad,};}
function layoutRows(rows){if(!rows.length)return[];var sectors={};var sectorNames=[];for(var i=0;i<rows.length;i++){var sector=rows[i].sector;if(!sectors[sector]){sectors[sector]=[];sectorNames.push(sector);}
sectors[sector].push(rows[i]);}
var sectorTotals=[];for(var sn=0;sn<sectorNames.length;sn++){var name=sectorNames[sn];var items=sectors[name];var total=0;for(var wi=0;wi<items.length;wi++)total+=items[wi].weight;sectorTotals.push({name:name,total:total});}
sectorTotals.sort(function(a,b){return b.total-a.total;});for(var sk in sectors){sectors[sk].sort(function(a,b){return b.weight-a.weight;});}
var canvas={x:0,y:0,w:100,h:100};var totals=[];for(var st=0;st<sectorTotals.length;st++){totals.push(sectorTotals[st].total);}
var sectorRects=squarify(totals,canvas);var layout=[];for(var si=0;si<sectorTotals.length;si++){var sname=sectorTotals[si].name;var srect=sectorRects[si];var sectorItems=sectors[sname];var padded=insetRect(srect,SECTOR_INSET_PCT);var weights=[];for(var ti=0;ti<sectorItems.length;ti++){weights.push(sectorItems[ti].weight);}
var tickerRects=squarify(weights,padded);for(var tj=0;tj<sectorItems.length;tj++){layout.push({row:sectorItems[tj],tile:tickerRects[tj]});}}
return layout;}
function tileShowsIdentifier(pxW,pxH){return((pxW>=LOGO_MIN_W&&pxH>=LOGO_MIN_H)||(pxW>=TEXT_MIN_W&&pxH>=TEXT_MIN_H));}
function tileShouldFold(tile,canvasW,canvasH){var pxW=(tile.w*canvasW)/100-FOLD_SAFETY_PX;var pxH=(tile.h*canvasH)/100-FOLD_SAFETY_PX;return!tileShowsIdentifier(pxW,pxH);}
function isElementVisible(el){if(!el)return false;try{var style=getComputedStyle(el);if(style.display==="none"||style.visibility==="hidden")return false;if(parseFloat(style.opacity)===0)return false;}catch(err){return false;}
var box=el.getBoundingClientRect();return box.width>0&&box.height>0;}
function realTileHasVisibleLabel(tileEl){if(!tileEl||tileEl.classList.contains("treemap__tile--aggregated")){return true;}
return(isElementVisible(tileEl.querySelector(".treemap__tile-logo"))||isElementVisible(tileEl.querySelector(".treemap__tile-text")));}
function collectUnlabeledAnchors(canvas){var anchors=[];var tiles=canvas.querySelectorAll("a.treemap__tile");for(var i=0;i<tiles.length;i++){if(!realTileHasVisibleLabel(tiles[i])){var href=tiles[i].getAttribute("href")||"";if(href.charAt(0)==="#")anchors.push(href.slice(1));}}
return anchors;}
function foldRowsByAnchors(rows,anchors,otherSector){if(!anchors.length)return rows;var toFold=[];for(var i=0;i<rows.length;i++){var row=rows[i];if(!row.foldedTickers&&anchors.indexOf(row.anchor)>=0){toFold.push(row);}}
if(!toFold.length)return rows;var remainingReal=[];for(var ri=0;ri<rows.length;ri++){var r=rows[ri];if(!r.foldedTickers&&toFold.indexOf(r)<0)remainingReal.push(r);}
if(!remainingReal.length)return rows;var next=[];for(var ni=0;ni<rows.length;ni++){if(toFold.indexOf(rows[ni])<0)next.push(rows[ni]);}
var batchWeight=0;var batchTickers=[];for(var bi=0;bi<toFold.length;bi++){batchWeight+=toFold[bi].weight;batchTickers.push(toFold[bi].ticker);}
var existingOther=null;for(var oi=0;oi<next.length;oi++){if(next[oi].foldedTickers){existingOther=next[oi];break;}}
if(!existingOther){next.push({ticker:"",name:"Other",sector:otherSector,weight:batchWeight,logoUrl:"",logoWFactor:1,logoHFactor:1,anchor:"",shortTicker:"",foldedTickers:batchTickers,});}else{existingOther.weight+=batchWeight;existingOther.foldedTickers=existingOther.foldedTickers.concat(batchTickers);}
return next;}
function mergeSmallIntoOther(rows,canvasW,canvasH,otherSector){var rowsList=rows.slice();var maxPasses=rowsList.length+1;for(var pass=0;pass<maxPasses;pass++){var layout=layoutRows(rowsList);var toFold=[];for(var li=0;li<layout.length;li++){var entry=layout[li];if(!entry.row.foldedTickers&&tileShouldFold(entry.tile,canvasW,canvasH)){toFold.push(entry.row);}}
if(!toFold.length)return rowsList;var remainingReal=[];for(var ri=0;ri<rowsList.length;ri++){var row=rowsList[ri];if(!row.foldedTickers&&toFold.indexOf(row)<0){remainingReal.push(row);}}
if(!remainingReal.length)return rowsList;var next=[];for(var ni=0;ni<rowsList.length;ni++){if(toFold.indexOf(rowsList[ni])<0)next.push(rowsList[ni]);}
var batchWeight=0;var batchTickers=[];for(var bi=0;bi<toFold.length;bi++){batchWeight+=toFold[bi].weight;batchTickers.push(toFold[bi].ticker);}
var existingOther=null;for(var oi=0;oi<next.length;oi++){if(next[oi].foldedTickers){existingOther=next[oi];break;}}
if(!existingOther){next.push({ticker:"",name:"Other",sector:otherSector,weight:batchWeight,logoUrl:"",logoWFactor:1,logoHFactor:1,anchor:"",shortTicker:"",foldedTickers:batchTickers,});}else{var mergedTickers=existingOther.foldedTickers.concat(batchTickers);existingOther.weight+=batchWeight;existingOther.foldedTickers=mergedTickers;}
rowsList=next;}
return rowsList;}
function sectorColor(sector,colorMap,otherSector,fallbackVar){if(colorMap[sector])return colorMap[sector];if(colorMap[otherSector])return colorMap[otherSector];return fallbackVar;}
function tileStyle(tile,sectorVar){return("left: "+
tile.x.toFixed(4)+"%; top: "+
tile.y.toFixed(4)+"%; width: "+
tile.w.toFixed(4)+"%; height: "+
tile.h.toFixed(4)+"%; background: var("+
sectorVar+");");}
function buildAggregatedTile(row,tile,meta,sectorVar){var labelPct=fmtPct(row.weight);var count=row.foldedTickers.length;var tickersBlurb=[];for(var i=0;i<row.foldedTickers.length;i++){tickersBlurb.push(stripExchange(row.foldedTickers[i]));}
var tooltip=meta.otherDisplayLabel+" ("+
count+" smaller holding"+
(count===1?"":"s")+"): "+
labelPct+"% - "+
tickersBlurb.join(", ");var div=document.createElement("div");div.className="treemap__tile treemap__tile--aggregated";div.setAttribute("role","img");div.setAttribute("data-sector",row.sector);div.style.cssText=tileStyle(tile,sectorVar);div.title=tooltip;div.setAttribute("aria-label",tooltip);var inner=document.createElement("span");inner.className="treemap__tile-inner";var text=document.createElement("span");text.className="treemap__tile-text";var ticker=document.createElement("span");ticker.className="treemap__tile-ticker";var long=document.createElement("span");long.className="treemap__tile-ticker-long";long.textContent=meta.otherDisplayLabel;var short=document.createElement("span");short.className="treemap__tile-ticker-short";short.textContent=meta.otherDisplayLabelShort;ticker.appendChild(long);ticker.appendChild(short);var weight=document.createElement("span");weight.className="treemap__tile-weight";weight.textContent=labelPct+"%";text.appendChild(ticker);text.appendChild(weight);inner.appendChild(text);div.appendChild(inner);return div;}
function buildRealTile(row,tile,sectorVar){var labelPct=fmtPct(row.weight);var tooltip=row.ticker+" - "+row.name+" ("+row.sector+"): "+labelPct+"%";var anchor=document.createElement("a");var modifier=row.logoUrl?"":" treemap__tile--no-logo";anchor.className="treemap__tile"+modifier;anchor.setAttribute("data-sector",row.sector);anchor.href="#"+row.anchor;anchor.style.cssText=tileStyle(tile,sectorVar);anchor.title=tooltip;anchor.setAttribute("aria-label",tooltip);var inner=document.createElement("span");inner.className="treemap__tile-inner";if(row.logoUrl){var img=document.createElement("img");img.className="treemap__tile-logo";img.src=row.logoUrl;img.alt=row.shortTicker;img.loading="lazy";img.decoding="async";img.width=48;img.height=24;img.style.cssText="--logo-w-factor: "+
row.logoWFactor.toFixed(3)+"; --logo-h-factor: "+
row.logoHFactor.toFixed(3)+";";img.onerror=function(){anchor.classList.add("treemap__tile--no-logo");img.style.display="none";};inner.appendChild(img);}
var text=document.createElement("span");text.className="treemap__tile-text";var tickerSpan=document.createElement("span");tickerSpan.className="treemap__tile-ticker";tickerSpan.textContent=row.shortTicker;var weight=document.createElement("span");weight.className="treemap__tile-weight";weight.textContent=labelPct+"%";text.appendChild(tickerSpan);text.appendChild(weight);inner.appendChild(text);anchor.appendChild(inner);return anchor;}
function buildLegend(rows,meta,colorMap,legendEl){var totals={};var order=[];for(var i=0;i<rows.length;i++){var sector=rows[i].sector;if(!totals[sector]){totals[sector]=0;order.push(sector);}
totals[sector]+=rows[i].weight;}
order.sort(function(a,b){return totals[b]-totals[a];});legendEl.textContent="";for(var si=0;si<order.length;si++){var sname=order[si];var sectorVar=sectorColor(sname,colorMap,meta.otherSector,"--treemap-color-other");var chip=document.createElement("span");chip.className="treemap__legend-chip";var swatch=document.createElement("span");swatch.className="treemap__legend-swatch";swatch.style.background="var("+sectorVar+")";var label=document.createElement("span");label.className="treemap__legend-label";label.textContent=sname;var weight=document.createElement("span");weight.className="treemap__legend-weight";weight.textContent=fmtPct(totals[sname])+"%";chip.appendChild(swatch);chip.appendChild(label);chip.appendChild(weight);legendEl.appendChild(chip);}}
function holdingsFromPayload(data){var holdings=data.holdings||[];var out=[];for(var i=0;i<holdings.length;i++){var h=holdings[i];out.push({ticker:h.ticker,name:h.name,sector:h.sector,weight:h.weight,logoUrl:h.logoUrl||"",logoWFactor:h.logoWFactor||1,logoHFactor:h.logoHFactor||1,anchor:h.anchor,shortTicker:h.shortTicker,foldedTickers:null,});}
return out;}
function paintTreemap(canvas,rows,meta,colorMap){var layout=layoutRows(rows);canvas.textContent="";for(var li=0;li<layout.length;li++){var entry=layout[li];var sectorVar=sectorColor(entry.row.sector,colorMap,meta.otherSector,"--treemap-color-other");var el;if(entry.row.foldedTickers){el=buildAggregatedTile(entry.row,entry.tile,meta,sectorVar);}else{el=buildRealTile(entry.row,entry.tile,sectorVar);}
canvas.appendChild(el);}
return layout;}
function layoutFigure(figure){var payloadEl=figure.querySelector(".treemap__payload");var canvas=figure.querySelector(".treemap__canvas");var legend=figure.querySelector(".treemap__legend");if(!payloadEl||!canvas||!legend)return;var data;try{data=JSON.parse(payloadEl.textContent||"");}catch(err){return;}
var rect=canvas.getBoundingClientRect();var canvasW=rect.width;var canvasH=rect.height;if(canvasW<=0||canvasH<=0)return;var meta={otherSector:data.otherSector||"Other",otherDisplayLabel:data.otherDisplayLabel||"Other equities",otherDisplayLabelShort:data.otherDisplayLabelShort||"Other",};var colorMap=data.sectorColors||{};var rows=holdingsFromPayload(data);rows.sort(function(a,b){return b.weight-a.weight;});var maxPasses=rows.length+2;for(var pass=0;pass<maxPasses;pass++){rows=mergeSmallIntoOther(rows,canvasW,canvasH,meta.otherSector);paintTreemap(canvas,rows,meta,colorMap);var unlabeled=collectUnlabeledAnchors(canvas);if(!unlabeled.length)break;rows=foldRowsByAnchors(rows,unlabeled,meta.otherSector);}
buildLegend(rows,meta,colorMap,legend);}
function debounce(fn,ms){var timer=null;return function(){if(timer)clearTimeout(timer);var self=this;var args=arguments;timer=setTimeout(function(){timer=null;fn.apply(self,args);},ms);};}
function boot(){var figures=document.querySelectorAll("figure.treemap");if(!figures.length)return;var relayout=debounce(function(figure){layoutFigure(figure);},80);for(var fi=0;fi<figures.length;fi++){var figure=figures[fi];var runLayout=function(){layoutFigure(figure);};if(typeof requestAnimationFrame==="function"){requestAnimationFrame(function(){requestAnimationFrame(runLayout);});}else{runLayout();}
if(typeof ResizeObserver!=="function")continue;var observer=new ResizeObserver(function(){relayout(figure);});observer.observe(figure);}}
if(document.readyState==="loading"){document.addEventListener("DOMContentLoaded",boot);}else{boot();}})();