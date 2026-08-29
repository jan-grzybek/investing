/*
 * Tiny inline script that strips the URL hash the moment the user
 * takes manual control of scrolling. The `Performance` / `Current`
 * / `Historical` nav links in the sticky header are plain in-page
 * anchors -- clicking `Current` appends `#current` to the URL and
 * the browser scrolls to that section. Without this script the hash
 * sticks around even after the user wheels elsewhere on the page, so
 * a subsequent refresh makes the browser re-jump to the section they
 * last clicked on instead of restoring their actual scroll position
 * -- which on a long holdings page reads as the page "scrolling down
 * uncontrollably" on every refresh.
 *
 * We only react to user-initiated input events (`wheel`,
 * `touchmove`, and the keys that scroll the page). That way the
 * initial smooth-scroll triggered by a nav click does NOT clear the
 * hash -- the hash stays in the URL while the user is "at" the
 * section they navigated to (so the link is still shareable), and
 * only gets dropped the instant the user starts exploring on their
 * own. Listeners are passive so they never block scrolling.
 *
 * Kept as a tight ES5-flavoured IIFE so the inline payload stays
 * small and gets a single stable SHA-256 hash (pinned in CSP).
 */
(function(){function clearHash(){if(!location.hash)return;history.replaceState(null,'',location.pathname+location.search);}var opts={passive:true};addEventListener('wheel',clearHash,opts);addEventListener('touchmove',clearHash,opts);addEventListener('keydown',function(e){var k=e.key;if(k==='ArrowDown'||k==='ArrowUp'||k==='PageDown'||k==='PageUp'||k==='Home'||k==='End'||k===' '||k==='Spacebar')clearHash();},opts);})();
