/*
 * Custom smooth-scroll for in-page anchor links. Native CSS
 * `scroll-behavior: smooth` is fast and abrupt, and on iOS Safari
 * the sticky header's `backdrop-filter` re-composites mid-scroll
 * which reads as a brief "blink" right after the tap. Driving the
 * scroll from JS lets us:
 *
 * * use an ease-out quartic animation that genuinely "slides"
 * between sections instead of snapping. `easeOutQuart`
 * (`1 - (1-t)^4`) front-loads motion: the scroll picks up
 * speed in the first frame and decelerates smoothly into the
 * target. The earlier `easeInOutCubic` curve started slow
 * (perceived as input lag), then accelerated through the
 * middle, then decelerated -- on a long page that "slow start,
 * fast middle, slow end" reads as "the page is stuttering and
 * then catching up" rather than as a smooth slide. Ease-out
 * also lets us shorten the overall duration without the
 * animation feeling rushed, since the user immediately sees
 * meaningful motion;
 * * cancel `preventDefault()` the anchor click so the browser
 * never performs the instant-jump that fights our animation;
 * * call `window.scrollTo` programmatically (which does NOT
 * fire wheel/touchmove), so the animation runs uninterrupted
 * while the existing `_HASH_CLEAR_SCRIPT` happily stays put;
 * * still write the section anchor into the URL via
 * `history.pushState` so the link is shareable, matching
 * pre-existing behaviour.
 *
 * The selector covers every same-page anchor on the page -- the
 * four nav links -- except for the visually-hidden `.skip-link`,
 * which assistive-tech users expect to jump instantly. Honours
 * `prefers-reduced-motion` by jumping directly to the target.
 *
 * Kept as a tight ES5-flavoured IIFE so the inline payload stays
 * small and gets a single stable SHA-256 hash (pinned in CSP).
 *
 * `slide` locks the destination `targetY(el)` at the moment of
 * the click and animates against it for the rest of the duration.
 * An earlier version re-read `targetY` on every frame to absorb
 * layout shifts in flight (iOS Safari URL-bar collapse, lazy logos
 * finishing decode), but with explicit `width`/`height` on
 * every holding logo there is no CLS to absorb, programmatic
 * `scrollTo` does not trigger the iOS URL-bar transition, and
 * the per-frame re-read introduced a subtle but visible jitter:
 * each `targetY` call rescales the entire trajectory, so any
 * sub-pixel shift was amplified through the ease curve into a
 * visible micro-stutter -- the user-reported "the animation
 * looks odd" feel on short hops from the allocation chart. A
 * single `scrollTo` to the current `targetY` at the very end
 * of the slide still catches any pixel-level layout drift that
 * happened in flight without contaminating the easing curve.
 *
 * `scrollTo` is invoked with `{behavior: 'auto'}` to
 * explicitly opt out of any user-agent / page CSS smooth scroll
 * that might otherwise layer a second animation on top of our
 * rAF loop.
 */
(function(){var rm=false;try{rm=matchMedia('(prefers-reduced-motion: reduce)').matches;}catch(e){}function ease(t){var u=1-t;return 1-u*u*u*u;}function sy(){return window.pageYOffset||document.documentElement.scrollTop||0;}var raf=null;function targetY(el){var r=el.getBoundingClientRect(),top=r.top+sy(),smt=0;try{smt=parseInt(getComputedStyle(el).scrollMarginTop,10)||0;}catch(e){}return Math.max(0,top-smt);}function jump(y){try{window.scrollTo({top:y,behavior:'auto'});}catch(e){window.scrollTo(0,y);}}function slide(el,d){if(raf!==null)cancelAnimationFrame(raf);var sy0=sy(),ty0=targetY(el),t0=null;function step(ts){if(t0===null)t0=ts;var t=Math.min(1,(ts-t0)/d);jump(sy0+(ty0-sy0)*ease(t));if(t<1){raf=requestAnimationFrame(step);}else{jump(targetY(el));raf=null;}}raf=requestAnimationFrame(step);}document.addEventListener('click',function(e){if(e.defaultPrevented)return;if(e.button!==0)return;if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;var t=e.target;if(!t||!t.closest)return;var a=t.closest('a[href^="#"]:not(.skip-link)');if(!a)return;var href=a.getAttribute('href');if(!href||href==='#')return;var el=document.getElementById(href.slice(1));if(!el)return;e.preventDefault();if(a.blur){try{a.blur();}catch(err){}}if(rm){jump(targetY(el));}else{var dist=Math.abs(targetY(el)-sy());var dur=Math.min(650,Math.max(280,dist*0.30));slide(el,dur);}try{history.pushState(null,'',href);}catch(err){}});})();
(function(){
/* Deferred: this file ships from <head>, so <body> is not parsed yet
   when it runs. The slide handler above gets away with running
   immediately because it delegates off ``document``; anything that
   reaches for a real element cannot. */
function boot(){
  var header=document.querySelector('.site-header');
  if(!header)return;
  /* The scroll offset for in-page anchors has to be the header's ACTUAL
     height. A hard-coded value was right on desktop and 18px short on a
     phone, where the nav wrapped to a second line -- so every section
     landed underneath the bar. Publishing the measured height lets the
     stylesheet's scroll-margin-top track it, which is the same number
     the slide animation reads back out of getComputedStyle. */
  function publish(){
    document.documentElement.style.setProperty('--header-h',Math.round(header.getBoundingClientRect().height)+'px');
  }
  publish();
  try{new ResizeObserver(publish).observe(header);}catch(e){window.addEventListener('resize',publish);}
  window.addEventListener('orientationchange',publish);

  /* Current-section state.
     The bar is sticky over one long document, so without this it is a
     map with no "you are here": a reader can jump but cannot tell
     where a jump landed, or where they have scrolled to since.
     The active section is the last one whose top has passed under the
     header -- the same offset the anchor scroll uses, so the pill
     lights exactly when the heading arrives rather than when the
     section's midpoint does.
     ``aria-current`` carries it, so the announcement and the paint
     come from one attribute. Reads are batched into a frame: this
     runs on every scroll event, and `getBoundingClientRect` in that
     loop is the one thing here that can cost a frame. */
  var spyNav=header.querySelector('.site-nav');
  if(spyNav){
    var links=[].slice.call(spyNav.querySelectorAll('a[href^="#"]'));
    var targets=links.map(function(a){
      try{return document.querySelector(a.getAttribute('href'));}catch(e){return null;}
    });
    var queued=false;
    var mark=function(){
      queued=false;
      var raw=getComputedStyle(document.documentElement).getPropertyValue('--header-h');
      var offset=(parseInt(raw,10)||52)+24;
      /* Default to the first section rather than to nothing: above
         every heading the reader is at the start of the document, and
         a bar with no pill lit reads as a broken feature rather than
         as "you are above the first section". */
      var best=0;
      targets.forEach(function(t,i){
        if(t&&t.getBoundingClientRect().top-offset<=0)best=i;
      });
      /* At the bottom of the document, the last section wins outright.
         The final section is shorter than the viewport, so its top
         never travels far enough up to cross the offset line -- scroll
         runs out first, and without this it could never be marked no
         matter how far the reader scrolled. */
      var doc=document.documentElement;
      if(window.innerHeight+window.scrollY>=doc.scrollHeight-2)best=links.length-1;
      links.forEach(function(a,i){
        if(i===best)a.setAttribute('aria-current','true');
        else a.removeAttribute('aria-current');
      });
    };
    if(links.length){
      mark();
      window.addEventListener('scroll',function(){
        if(queued)return;
        queued=true;
        requestAnimationFrame(mark);
      },{passive:true});
      window.addEventListener('resize',function(){
        if(queued)return;
        queued=true;
        requestAnimationFrame(mark);
      },{passive:true});
    }
  }

  var toggle=header.querySelector('.site-nav__toggle');
  var nav=header.querySelector('.site-nav');
  if(!toggle||!nav)return;
  function setOpen(open){
    toggle.setAttribute('aria-expanded',open?'true':'false');
    header.setAttribute('data-nav',open?'open':'closed');
  }
  setOpen(false);
  toggle.addEventListener('click',function(e){
    e.stopPropagation();
    setOpen(toggle.getAttribute('aria-expanded')!=='true');
  });
  /* An overlay that only closes from the button it opened with is a
     trap on a phone; tapping the page behind it is the gesture a
     reader will try first. */
  document.addEventListener('click',function(e){
    if(!header.contains(e.target))setOpen(false);
  });
  /* Close on choose: leaving it open would cover the section the
     reader just asked to see. */
  nav.addEventListener('click',function(e){
    if(e.target.closest('a'))setOpen(false);
  });
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape')setOpen(false);
  });
}
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',boot);}else{boot();}
})();
