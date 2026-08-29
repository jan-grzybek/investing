/*
 * Click-to-sort behaviour for the "Trades" table.
 *
 * Each `<tr class="trades__row">` carries the sort keys it can be
 * ordered by on `data-sort-*` attributes (date / ticker / name /
 * action / detail). The script wires every `<th data-sort-key="...">`
 * so a click on the inner `.trades__sort` button:
 *
 * * toggles the direction when the same column is clicked twice in
 * a row (asc <-> desc);
 * * picks a sensible initial direction the first time the user lands
 * on a column -- "desc" for date (newest first matches the way the
 * section was already ordered by default), "asc" for everything
 * else (alphabetical A -> Z for ticker / name, BUY before SELL for
 * action, OPEN -> CLOSE for detail);
 * * updates `aria-sort` on the active `<th>` so screen readers
 * announce the new state, and resets the other columns to "none"
 * so only one indicator triangle ever reads as active;
 * * keeps a deterministic tie-break (date desc, then ticker asc) so
 * equal-key rows always reorder the same way and the table doesn't
 * visibly shuffle when the user sorts by action and several rows
 * share a label.
 *
 * Bursts span multiple days but only one `data-sort-date` value is
 * emitted per row (the burst's `end_date`, i.e. its most recent
 * event) -- it's the natural anchor for the "when did this trade
 * happen?" question and matches the desktop convention of headlining
 * a burst by its last fill.
 *
 * Right after the initial sort the script also runs `freezeColumns`
 * to pin each `<th>` to the width it would naturally take with
 * every row exposed. The default `table-layout: auto` recomputes
 * column widths from whichever rows are currently visible, and the
 * "Show fewer entries" cap (CSS hides `tr:nth-of-type(n+11)`) means
 * sorting can rotate a long name -- "UnitedHealth Group Inc.", "Lam
 * Research Corporation" -- in or out of the top-10 window, which
 * visibly squashes or widens the Company column. By measuring once
 * with all rows displayed and then locking the table to
 * `table-layout: fixed` with those pixel widths, the column edges
 * stay flush across every sort + collapse permutation. On resize
 * the handler does two things, in this order: it first runs
 * `unfreeze` synchronously -- clearing `table-layout: fixed` and
 * all per-`<th>` pixel widths -- so the table reflows naturally
 * with the new wrap dimensions as the user drags the window edge
 * (without this immediate relax, the table would stay pinned to its
 * previous wider column widths and visibly overflow the wrap until
 * the debounced re-measure caught up, which is exactly the
 * "everything jumps at once" effect the redesign exists to avoid).
 * A 150ms debounce then re-runs `freezeColumns` to lock the new
 * natural widths in for the next sort/expand cycle.
 *
 * Responsive column hiding is handled by the stylesheet now, not the
 * script: `.trades__wrap` is declared as a named `trades`
 * inline-size container, and the matching `@container trades
 * (max-width: ...)` rules drop the Company column at ~600px wrap
 * width and the Action pill at ~430px wrap width. Doing the visibility
 * decision in CSS rather than JS means every resize frame gets the
 * correct column set as a synchronous side-effect of layout, without
 * the debounce delay an earlier JS-driven version had between the
 * user crossing a threshold and the column actually disappearing.
 * The two thresholds are 170px apart, so a continuous resize through
 * the boundary produces two clearly separated visual transitions --
 * Company drops first, the 5-column layout survives all the way down
 * to phone widths, and only on the narrowest viewports does Action
 * follow. `freezeColumns` re-runs on resize so the locked pixel
 * widths refresh once a column has dropped out and the remaining
 * columns redistribute.
 *
 * `boot` is deferred to `DOMContentLoaded` because the script ships
 * from <head> and the `<table class="trades">` body it queries for
 * isn't parsed yet at that point. Without the defer the IIFE would
 * observe a null table on every page load and bail out, leaving the
 * sort headers silently inert (which is exactly the bug we're fixing
 * here). The pattern matches `_RETURN_CHART_SCRIPT` further up.
 *
 * Kept as a tight ES5-flavoured IIFE so the inline payload stays
 * small and gets a single stable SHA-256 hash (pinned in CSP).
 */
(function(){function boot(){var table=document.querySelector('table.trades');if(!table)return;var tbody=table.querySelector('tbody');if(!tbody)return;var wrap=table.closest('.trades__wrap');var ths=table.querySelectorAll('th[data-sort-key]');var allThs=table.querySelectorAll('thead th');var state={key:null,dir:null};function rowKey(row,key){return row.getAttribute('data-sort-'+key)||'';}function cmp(a,b,key){var av=rowKey(a,key),bv=rowKey(b,key);if(key==='price'){var ac=rowKey(a,'currency'),bc=rowKey(b,'currency');if(ac!==bc)return ac<bc?-1:1;av=parseFloat(av);bv=parseFloat(bv);if(av<bv)return -1;if(av>bv)return 1;return 0;}if(key==='action'||key==='detail'){av=parseInt(av,10);bv=parseInt(bv,10);if(av<bv)return -1;if(av>bv)return 1;return 0;}if(av<bv)return -1;if(av>bv)return 1;return 0;}function sortBy(key,dir){var rows=Array.prototype.slice.call(tbody.querySelectorAll('tr'));rows.sort(function(a,b){var c=cmp(a,b,key);if(dir==='desc')c=-c;if(c!==0)return c;var ad=rowKey(a,'date'),bd=rowKey(b,'date');if(ad!==bd)return ad<bd?1:-1;var at=rowKey(a,'ticker'),bt=rowKey(b,'ticker');if(at<bt)return -1;if(at>bt)return 1;return 0;});for(var i=0;i<rows.length;i++)tbody.appendChild(rows[i]);for(var j=0;j<ths.length;j++){var th=ths[j];var k=th.getAttribute('data-sort-key');if(k===key){th.setAttribute('aria-sort',dir==='asc'?'ascending':'descending');}else{th.setAttribute('aria-sort','none');}}state.key=key;state.dir=dir;}/* Measuring has to happen on `auto`, or the measurement just reads
     back the baseline percentages the stylesheet declares and the
     freeze can never adapt to the actual content. Clearing the
     inline value is not enough now that `table-layout: fixed` also
     lives in the CSS, so this sets `auto` explicitly.
     Safe because freezeColumns() restores `fixed` before it
     returns: the two happen in one synchronous call, so the
     browser never paints the table on auto layout. */
  function unfreeze(){table.style.tableLayout='auto';for(var i=0;i<allThs.length;i++)allThs[i].style.width='';}/* Freeze the column *proportions*, not their pixel widths.
     Widths were pinned in absolute px so a re-sort could not reflow
     the table. That works until the window changes size: the resize
     handler called unfreeze(), which clears table-layout, and
     re-measured 150ms later. In between the table was back on auto
     layout, where its min-content is wider than the card -- the
     columns jumped, a scrollbar appeared inside the card and the
     Price column clipped its currency code.
     Percentages hold the same proportions through any width, so the
     freeze survives a resize and the unfreeze/refreeze cycle goes
     away entirely. */
  function freezeColumns(){var wasExpanded=table.getAttribute('data-expanded')==='true';if(!wasExpanded)table.setAttribute('data-expanded','true');unfreeze();var widths=[];for(var i=0;i<allThs.length;i++)widths.push(allThs[i].getBoundingClientRect().width);if(!wasExpanded)table.removeAttribute('data-expanded');var total=0;for(var t=0;t<widths.length;t++)total+=widths[t];if(!(total>0))return;table.style.tableLayout='fixed';for(var w=0;w<allThs.length;w++)allThs[w].style.width=widths[w]>0?(widths[w]/total*100).toFixed(4)+'%':'0';}table.addEventListener('click',function(e){var t=e.target;if(!t||!t.closest)return;var btn=t.closest('.trades__sort');if(!btn)return;var th=btn.closest('th[data-sort-key]');if(!th)return;var key=th.getAttribute('data-sort-key');var dir;if(state.key===key){dir=state.dir==='asc'?'desc':'asc';}else{dir=th.getAttribute('data-sort-kind')==='number'?'desc':'asc';}sortBy(key,dir);});var ik=table.getAttribute('data-sort-default')||'date';var id=table.getAttribute('data-sort-default-dir')||'desc';sortBy(ik,id);/* Column widths belong to the table layout, and below the
     container query's threshold there is no table layout: the
     header row is a scrolling strip of sort chips and each chip
     sizes itself to its own label.
     Freezing there was actively harmful. The percentages are
     shares of the *strip*, and the strip is narrower than the
     chips it scrolls, so every chip was squeezed to about 70% of
     its label and the buttons overlapped their neighbours -- the
     horizontal scroll exists precisely so they do not have to.
     So: freeze in table mode, clear in card mode, and re-sync
     when the container query flips between them. Both paths are
     synchronous, so the table is never painted on auto layout --
     which is the failure the freeze exists to prevent. */
  function syncColumns(){
    if(getComputedStyle(table).display!=='table'){unfreeze();table.style.tableLayout='';return;}
    freezeColumns();
  }
  syncColumns();
  try{new ResizeObserver(syncColumns).observe(wrap||table);}catch(e){window.addEventListener('resize',syncColumns);}/* No resize handler. Percentage widths already track the new
     measure, and the unfreeze() that used to run here is exactly
     what let the table escape its card mid-resize. */var toggle=document.querySelector('.trades__toggle');if(toggle){var total=toggle.getAttribute('data-total')||'';var showLabel='Show all '+total+' entries';var hideLabel='Show fewer entries';toggle.addEventListener('click',function(){var open=table.getAttribute('data-expanded')==='true';if(open){table.removeAttribute('data-expanded');toggle.setAttribute('aria-expanded','false');toggle.textContent=showLabel;}else{table.setAttribute('data-expanded','true');toggle.setAttribute('aria-expanded','true');toggle.textContent=hideLabel;}});}}if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',boot);}else{boot();}})();
