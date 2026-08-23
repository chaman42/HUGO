// armor-svg-grid.js — Remaining armor model SVG definitions and the armor grid/detail-view rendering.
function _model8SVG() {
  return `<svg class="armor-silhouette model8-blueprint" viewBox="0 0 400 800"
    fill="none" stroke="#4db8ff" xmlns="http://www.w3.org/2000/svg">
    <!-- Helmet — more angular/refined faceting than Model VI -->
    <path stroke-width="2.5" d="M200,28 L232,42 L246,72 L240,98 L226,124 L200,140 L174,124 L160,98 L154,72 L168,42 Z"/>
    <!-- Visor slit (thinner/more precise than Model VI's band) -->
    <path stroke-width="2.5" d="M172,82 L228,82 L228,90 L172,90 Z"/>
    <line stroke-width="2" x1="200" y1="82"  x2="200" y2="90"/>
    <line stroke-width="2" x1="158" y1="72"  x2="242" y2="72"/>
    <line stroke-width="2" x1="168" y1="100" x2="232" y2="100"/>
    <line stroke-width="2" x1="178" y1="120" x2="222" y2="120"/>
    <!-- Neck -->
    <path stroke-width="2.5" d="M183,140 L217,140 L213,160 L187,160 Z"/>
    <!-- Shoulder pauldrons — large, structured, with an added facet line -->
    <path stroke-width="2.5" d="M85,170 L184,158 L184,226 L98,234 L68,202 Z"/>
    <path stroke-width="2.5" d="M315,170 L216,158 L216,226 L302,234 L332,202 Z"/>
    <line stroke-width="2" x1="100" y1="180" x2="178" y2="195"/>
    <line stroke-width="2" x1="300" y1="180" x2="222" y2="195"/>
    <line stroke-width="2" x1="80"  y1="228" x2="165" y2="224"/>
    <line stroke-width="2" x1="235" y1="224" x2="320" y2="228"/>
    <!-- Chest / torso outline -->
    <path stroke-width="2.5" d="M184,158 L216,158 L262,192 L259,330 L200,352 L141,330 L138,192 Z"/>
    <!-- Collar seam -->
    <line stroke-width="2" x1="158" y1="168" x2="242" y2="168"/>
    <!-- Central reactor: triangle pointing down, with an inner outline for depth -->
    <path stroke-width="2.5" d="M183,200 L217,200 L200,252 Z"/>
    <path stroke-width="2"   d="M191,212 L209,212 L200,242 Z"/>
    <!-- Chest facets converging on the reactor — the centerpiece geometric pattern -->
    <line stroke-width="2" x1="183" y1="200" x2="150" y2="182"/>
    <line stroke-width="2" x1="217" y1="200" x2="250" y2="182"/>
    <line stroke-width="2" x1="183" y1="200" x2="145" y2="225"/>
    <line stroke-width="2" x1="217" y1="200" x2="255" y2="225"/>
    <line stroke-width="2" x1="150" y1="182" x2="145" y2="225"/>
    <line stroke-width="2" x1="250" y1="182" x2="255" y2="225"/>
    <!-- Segmented abdominal section below the reactor -->
    <line stroke-width="2" x1="160" y1="270" x2="240" y2="270"/>
    <line stroke-width="2" x1="163" y1="292" x2="237" y2="292"/>
    <line stroke-width="2" x1="166" y1="314" x2="234" y2="314"/>
    <line stroke-width="2" x1="141" y1="328" x2="259" y2="328"/>
    <!-- Waist -->
    <path stroke-width="2.5" d="M141,330 L200,352 L259,330 L251,400 L200,421 L149,400 Z"/>
    <!-- Biceps -->
    <path stroke-width="2.5" d="M68,225 L98,234 L107,240 L104,315 L66,318 L58,242 Z"/>
    <path stroke-width="2.5" d="M332,225 L302,234 L293,240 L296,315 L334,318 L342,242 Z"/>
    <line stroke-width="2" x1="61"  y1="316" x2="106" y2="313"/>
    <line stroke-width="2" x1="294" y1="313" x2="339" y2="316"/>
    <!-- Forearms — slightly boxier (straighter, less taper) than Model VI -->
    <path stroke-width="2.5" d="M66,318 L104,315 L102,410 L68,412 Z"/>
    <path stroke-width="2.5" d="M334,318 L296,315 L298,410 L332,412 Z"/>
    <line stroke-width="2" x1="70"  y1="360" x2="100" y2="358"/>
    <line stroke-width="2" x1="300" y1="358" x2="330" y2="360"/>
    <line stroke-width="2" x1="69"  y1="408" x2="101" y2="405"/>
    <line stroke-width="2" x1="299" y1="405" x2="331" y2="408"/>
    <!-- Gauntlets -->
    <path stroke-width="2.5" d="M69,408 L101,405 L99,448 L86,461 L67,453 L65,418 Z"/>
    <path stroke-width="2.5" d="M331,408 L299,405 L301,448 L314,461 L333,453 L335,418 Z"/>
    <line stroke-width="2" x1="68"  y1="428" x2="98"  y2="426"/>
    <line stroke-width="2" x1="70"  y1="440" x2="96"  y2="438"/>
    <line stroke-width="2" x1="302" y1="426" x2="332" y2="428"/>
    <line stroke-width="2" x1="304" y1="438" x2="330" y2="440"/>
    <!-- Thighs -->
    <path stroke-width="2.5" d="M149,400 L199,421 L196,556 L153,559 L147,423 Z"/>
    <path stroke-width="2.5" d="M251,400 L201,421 L204,556 L247,559 L253,423 Z"/>
    <line stroke-width="2" x1="152" y1="480" x2="197" y2="478"/>
    <line stroke-width="2" x1="203" y1="478" x2="248" y2="480"/>
    <line stroke-width="2" x1="151" y1="557" x2="197" y2="554"/>
    <line stroke-width="2" x1="203" y1="554" x2="249" y2="557"/>
    <!-- Shins -->
    <path stroke-width="2.5" d="M153,559 L196,556 L193,676 L159,679 Z"/>
    <path stroke-width="2.5" d="M247,559 L204,556 L207,676 L241,679 Z"/>
    <line stroke-width="2" x1="156" y1="615" x2="194" y2="613"/>
    <line stroke-width="2" x1="206" y1="613" x2="244" y2="615"/>
    <line stroke-width="2" x1="157" y1="677" x2="195" y2="675"/>
    <line stroke-width="2" x1="205" y1="675" x2="243" y2="677"/>
    <!-- Boots, with a defined sole line -->
    <path stroke-width="2.5" d="M159,679 L193,676 L197,731 L186,756 L141,756 L136,716 Z"/>
    <path stroke-width="2.5" d="M241,679 L207,676 L203,731 L214,756 L259,756 L264,716 Z"/>
    <line stroke-width="2" x1="140" y1="700" x2="193" y2="698"/>
    <line stroke-width="2" x1="207" y1="698" x2="260" y2="700"/>
    <line stroke-width="2" x1="136" y1="749" x2="197" y2="749"/>
    <line stroke-width="2" x1="203" y1="749" x2="264" y2="749"/>
  </svg>`
}

// ── Model X (Infinity) custom blueprint diagram — same rationale as Model
// VI/VIII above (bespoke silhouette instead of the generic placeholder,
// only for this one model; hardcoded #4db8ff (brightened from the base #3fa9f5, same as Model VI), never currentColor). The
// most advanced/aggressive of the three: wider shoulder stance, a small
// vertical-oval reactor inside a diamond frame (vs VI's plain circle and
// VIII's downward triangle) with sharp diagonal lines converging on it
// instead of VIII's more symmetric kite facets, boxier closed-fist
// gauntlets, small rect "tech detail" marks on the forearms, and more
// panel lines per segment than either earlier model throughout. -->
function _model10SVG() {
  return `<svg class="armor-silhouette model10-blueprint" viewBox="0 0 400 800"
    fill="none" stroke="#4db8ff" xmlns="http://www.w3.org/2000/svg">
    <!-- Helmet — sharper peak, more aggressive faceting than Model VI/VIII -->
    <path stroke-width="2.5" d="M200,22 L228,38 L244,68 L236,96 L220,122 L200,138 L180,122 L164,96 L156,68 L172,38 Z"/>
    <!-- Distinctive separate eye slits (not one visor band) -->
    <path stroke-width="2.5" d="M172,76 L196,76 L194,86 L174,86 Z"/>
    <path stroke-width="2.5" d="M204,76 L228,76 L226,86 L206,86 Z"/>
    <line stroke-width="2" x1="160" y1="66"  x2="240" y2="66"/>
    <line stroke-width="2" x1="168" y1="100" x2="232" y2="100"/>
    <line stroke-width="2" x1="178" y1="118" x2="222" y2="118"/>
    <!-- Neck -->
    <path stroke-width="2.5" d="M183,138 L217,138 L213,158 L187,158 Z"/>
    <!-- Shoulder pauldrons — wider stance, sharper than Model VIII -->
    <path stroke-width="2.5" d="M60,168 L182,156 L182,226 L92,236 L48,198 Z"/>
    <path stroke-width="2.5" d="M340,168 L218,156 L218,226 L308,236 L352,198 Z"/>
    <line stroke-width="2" x1="78"  y1="178" x2="172" y2="192"/>
    <line stroke-width="2" x1="322" y1="178" x2="228" y2="192"/>
    <line stroke-width="2" x1="74"  y1="230" x2="160" y2="224"/>
    <line stroke-width="2" x1="240" y1="224" x2="326" y2="230"/>
    <!-- Chest / torso outline -->
    <path stroke-width="2.5" d="M182,156 L218,156 L264,190 L261,330 L200,354 L139,330 L136,190 Z"/>
    <line stroke-width="2" x1="156" y1="166" x2="244" y2="166"/>
    <!-- Reactor: small vertical oval inside a diamond frame -->
    <path stroke-width="2.5" d="M200,193 L223,219 L200,246 L177,219 Z"/>
    <ellipse stroke-width="2" cx="200" cy="219" rx="10" ry="18"/>
    <!-- Sharp diagonal lines converging on the reactor -->
    <line stroke-width="2" x1="145" y1="178" x2="200" y2="193"/>
    <line stroke-width="2" x1="255" y1="178" x2="200" y2="193"/>
    <line stroke-width="2" x1="134" y1="248" x2="177" y2="219"/>
    <line stroke-width="2" x1="266" y1="248" x2="223" y2="219"/>
    <line stroke-width="2" x1="200" y1="246" x2="200" y2="288"/>
    <!-- Diagonal accents suggesting layered plating -->
    <line stroke-width="2" x1="152" y1="198" x2="188" y2="226"/>
    <line stroke-width="2" x1="248" y1="198" x2="212" y2="226"/>
    <!-- Segmented abdominal section — most segments of the three models -->
    <line stroke-width="2" x1="158" y1="266" x2="242" y2="266"/>
    <line stroke-width="2" x1="162" y1="284" x2="238" y2="284"/>
    <line stroke-width="2" x1="165" y1="302" x2="235" y2="302"/>
    <line stroke-width="2" x1="168" y1="320" x2="232" y2="320"/>
    <line stroke-width="2" x1="170" y1="275" x2="195" y2="293"/>
    <line stroke-width="2" x1="230" y1="275" x2="205" y2="293"/>
    <line stroke-width="2" x1="139" y1="330" x2="261" y2="330"/>
    <!-- Waist -->
    <path stroke-width="2.5" d="M139,330 L200,354 L261,330 L253,402 L200,423 L147,402 Z"/>
    <!-- Biceps -->
    <path stroke-width="2.5" d="M48,224 L92,236 L102,242 L99,317 L58,320 L38,244 Z"/>
    <path stroke-width="2.5" d="M352,224 L308,236 L298,242 L301,317 L342,320 L362,244 Z"/>
    <line stroke-width="2" x1="45"  y1="260" x2="90"  y2="255"/>
    <line stroke-width="2" x1="310" y1="255" x2="355" y2="260"/>
    <line stroke-width="2" x1="52"  y1="318" x2="101" y2="315"/>
    <line stroke-width="2" x1="299" y1="315" x2="348" y2="318"/>
    <!-- Forearms — extra rect "tech detail" marks suggesting circuitry underneath -->
    <path stroke-width="2.5" d="M58,320 L99,317 L96,412 L62,415 Z"/>
    <path stroke-width="2.5" d="M342,320 L301,317 L304,412 L338,415 Z"/>
    <line stroke-width="2" x1="64" y1="362" x2="94"  y2="360"/>
    <line stroke-width="2" x1="306" y1="360" x2="336" y2="362"/>
    <rect stroke-width="2" x="68" y="375" width="20" height="10"/>
    <rect stroke-width="2" x="312" y="375" width="20" height="10"/>
    <line stroke-width="2" x1="61"  y1="410" x2="93"  y2="407"/>
    <line stroke-width="2" x1="307" y1="407" x2="339" y2="410"/>
    <!-- Gauntlets — slightly closed fists, more compact than an open gauntlet -->
    <path stroke-width="2.5" d="M61,410 L93,407 L91,443 L80,458 L64,452 L58,420 Z"/>
    <path stroke-width="2.5" d="M339,410 L307,407 L309,443 L320,458 L336,452 L342,420 Z"/>
    <line stroke-width="2" x1="62"  y1="428" x2="90"  y2="426"/>
    <line stroke-width="2" x1="310" y1="426" x2="338" y2="428"/>
    <!-- Thighs -->
    <path stroke-width="2.5" d="M147,402 L198,423 L195,558 L151,561 L145,425 Z"/>
    <path stroke-width="2.5" d="M253,402 L202,423 L205,558 L249,561 L255,425 Z"/>
    <line stroke-width="2" x1="150" y1="460" x2="193" y2="455"/>
    <line stroke-width="2" x1="207" y1="455" x2="250" y2="460"/>
    <line stroke-width="2" x1="149" y1="559" x2="196" y2="556"/>
    <line stroke-width="2" x1="204" y1="556" x2="251" y2="559"/>
    <!-- Shins -->
    <path stroke-width="2.5" d="M151,561 L195,558 L191,678 L157,681 Z"/>
    <path stroke-width="2.5" d="M249,561 L205,558 L209,678 L243,681 Z"/>
    <line stroke-width="2" x1="154" y1="610" x2="192" y2="607"/>
    <line stroke-width="2" x1="208" y1="607" x2="246" y2="610"/>
    <line stroke-width="2" x1="155" y1="679" x2="193" y2="677"/>
    <line stroke-width="2" x1="207" y1="677" x2="245" y2="679"/>
    <!-- Boots, with a defined sole line -->
    <path stroke-width="2.5" d="M157,681 L191,677 L196,733 L184,758 L138,758 L133,718 Z"/>
    <path stroke-width="2.5" d="M243,681 L209,677 L204,733 L216,758 L262,758 L267,718 Z"/>
    <line stroke-width="2" x1="137" y1="702" x2="191" y2="700"/>
    <line stroke-width="2" x1="209" y1="700" x2="263" y2="702"/>
    <line stroke-width="2" x1="133" y1="751" x2="196" y2="751"/>
    <line stroke-width="2" x1="204" y1="751" x2="267" y2="751"/>
  </svg>`
}

// ── Model VII custom blueprint diagram — same rationale as Model VI/VIII/X
// above (bespoke silhouette instead of the generic placeholder, only for
// this one model; hardcoded #4db8ff (brightened from the base #3fa9f5, same as Model VI), never currentColor). The most
// Mark-7-faithful diagram in the series — a rounded dome helmet (the only
// one of the four using actual curves rather than pure angular facets),
// a circular double-ring reactor with V-shaped pec lines converging on it,
// rounded pauldrons, and knee-guard/toe-cap details the other three don't
// have. Deliberately NOT a trace: reactor sits a touch lower than the real
// Mark 7's, the pauldron outer edge is a shallower, less circular curve,
// and the thigh/shin seam is a diagonal cut instead of a straight one —
// meant to read as "clearly inspired by," not "copied from." -->
function _model7SVG() {
  return `<svg class="armor-silhouette model7-blueprint" viewBox="0 0 400 800"
    fill="none" stroke="#4db8ff" xmlns="http://www.w3.org/2000/svg">
    <!-- Helmet — rounded dome top, angular cheekpieces, defined chin guard -->
    <path stroke-width="2.5" d="M160,70 A42,46 0 0 1 240,70 L235,96 L221,122 L200,134 L179,122 L165,96 Z"/>
    <!-- Narrow horizontal eye slits -->
    <path stroke-width="2.5" d="M168,88 L194,88 L194,94 L168,94 Z"/>
    <path stroke-width="2.5" d="M206,88 L232,88 L232,94 L206,94 Z"/>
    <!-- Neck piece, with a defined mid-neck seam -->
    <path stroke-width="2.5" d="M182,134 L218,134 L214,156 L186,156 Z"/>
    <line stroke-width="2" x1="184" y1="145" x2="216" y2="145"/>
    <!-- Shoulders: rounded Mark-7-style pauldrons (curved outer edge, unlike
         Model VI/VIII/X's sharp angular point), with a raised edge line -->
    <path stroke-width="2.5" d="M90,168 L184,154 L184,222 L102,226 Q78,222 78,198 Q78,180 90,168 Z"/>
    <path stroke-width="2.5" d="M310,168 L216,154 L216,222 L298,226 Q322,222 322,198 Q322,180 310,168 Z"/>
    <line stroke-width="2" x1="95"  y1="178" x2="175" y2="168"/>
    <line stroke-width="2" x1="305" y1="178" x2="225" y2="168"/>
    <line stroke-width="2" x1="85"  y1="224" x2="165" y2="220"/>
    <line stroke-width="2" x1="235" y1="220" x2="315" y2="224"/>
    <!-- Chest / torso outline -->
    <path stroke-width="2.5" d="M182,154 L218,154 L258,186 L255,328 L200,350 L145,328 L142,186 Z"/>
    <line stroke-width="2" x1="168" y1="162" x2="232" y2="162"/>
    <!-- Iconic V-shaped pec lines converging on the reactor -->
    <line stroke-width="2" x1="160" y1="170" x2="200" y2="215"/>
    <line stroke-width="2" x1="240" y1="170" x2="200" y2="215"/>
    <!-- Reactor — double circle outline, sitting a touch lower than the real
         Mark 7's for a deliberate, recognizable difference -->
    <circle stroke-width="2.5" cx="200" cy="235" r="24"/>
    <circle stroke-width="2"   cx="200" cy="235" r="15"/>
    <!-- Abdominal bands, a slight taper toward the waist -->
    <line stroke-width="2" x1="165" y1="275" x2="235" y2="273"/>
    <line stroke-width="2" x1="168" y1="297" x2="232" y2="295"/>
    <line stroke-width="2" x1="171" y1="317" x2="229" y2="315"/>
    <line stroke-width="2" x1="145" y1="328" x2="255" y2="328"/>
    <!-- Waist, tapered -->
    <path stroke-width="2.5" d="M145,330 L200,350 L255,330 L247,398 L200,419 L153,398 Z"/>
    <!-- Biceps -->
    <path stroke-width="2.5" d="M85,222 L100,228 L108,234 L105,312 L70,315 L63,236 Z"/>
    <path stroke-width="2.5" d="M315,222 L300,228 L292,234 L295,312 L330,315 L337,236 Z"/>
    <line stroke-width="2" x1="66"  y1="313" x2="107" y2="310"/>
    <line stroke-width="2" x1="293" y1="310" x2="334" y2="313"/>
    <!-- Forearms — slight flare toward the wrist -->
    <path stroke-width="2.5" d="M70,315 L105,312 L110,410 L72,414 Z"/>
    <path stroke-width="2.5" d="M330,315 L295,312 L290,410 L328,414 Z"/>
    <line stroke-width="2" x1="73"  y1="360" x2="107" y2="357"/>
    <line stroke-width="2" x1="293" y1="357" x2="327" y2="360"/>
    <!-- Gauntlet, with knuckle lines -->
    <path stroke-width="2.5" d="M72,414 L109,410 L107,452 L94,466 L74,458 L68,420 Z"/>
    <path stroke-width="2.5" d="M328,414 L291,410 L293,452 L306,466 L326,458 L332,420 Z"/>
    <line stroke-width="2" x1="76" y1="432" x2="105" y2="429"/>
    <line stroke-width="2" x1="78" y1="444" x2="103" y2="441"/>
    <line stroke-width="2" x1="295" y1="429" x2="324" y2="432"/>
    <line stroke-width="2" x1="297" y1="441" x2="322" y2="444"/>
    <!-- Thighs -->
    <path stroke-width="2.5" d="M153,398 L199,419 L195,552 L154,558 L149,421 Z"/>
    <path stroke-width="2.5" d="M247,398 L201,419 L205,552 L246,558 L251,421 Z"/>
    <!-- Knee guards -->
    <path stroke-width="2.5" d="M158,542 L172,536 L184,545 L172,556 Z"/>
    <path stroke-width="2.5" d="M242,542 L228,536 L216,545 L228,556 Z"/>
    <!-- Thigh/shin seam — a diagonal cut, unlike the near-horizontal
         division lines on the other three models -->
    <line stroke-width="2" x1="152" y1="556" x2="197" y2="548"/>
    <line stroke-width="2" x1="248" y1="556" x2="203" y2="548"/>
    <!-- Shins -->
    <path stroke-width="2.5" d="M154,558 L195,552 L191,674 L159,678 Z"/>
    <path stroke-width="2.5" d="M246,558 L205,552 L209,674 L241,678 Z"/>
    <line stroke-width="2" x1="157" y1="612" x2="193" y2="608"/>
    <line stroke-width="2" x1="207" y1="608" x2="243" y2="612"/>
    <line stroke-width="2" x1="158" y1="676" x2="192" y2="673"/>
    <line stroke-width="2" x1="208" y1="673" x2="242" y2="676"/>
    <!-- Boots, with a defined toe cap and sole line -->
    <path stroke-width="2.5" d="M159,678 L191,673 L195,728 L184,753 L138,753 L134,714 Z"/>
    <path stroke-width="2.5" d="M241,678 L209,673 L205,728 L216,753 L262,753 L266,714 Z"/>
    <line stroke-width="2" x1="138" y1="735" x2="184" y2="733"/>
    <line stroke-width="2" x1="216" y1="733" x2="262" y2="735"/>
    <line stroke-width="2" x1="134" y1="747" x2="195" y2="747"/>
    <line stroke-width="2" x1="205" y1="747" x2="266" y2="747"/>
  </svg>`
}

// ── Model-specific blueprint diagram, falling back to the generic
// silhouette — shared by the grid cards (_renderArmorGrid) and the detail
// view (_openDetail) so the two never drift out of sync on which models
// have a bespoke diagram (currently VI, VII, VIII, X). ───────────────────
function _armorDiagramSVG(id) {
  switch (id) {
    case 'model-6':  return _model6SVG()
    case 'model-7':  return _model7SVG()
    case 'model-8':  return _model8SVG()
    case 'model-10': return _model10SVG()
    default:         return _suitSVG()
  }
}

// ── Badge CSS class from status string ─────────────────────────────────────
function _badgeClass(status) {
  switch (status) {
    case 'COMPLETADO':      return 'badge-completado'
    case 'NO COMPLETADO':   return 'badge-no-completado'   // legacy value (model-7) — not offered by the status picker, kept renderable
    case 'EN CONSTRUCCIÓN': return 'badge-construccion'
    case 'EN REPARACIÓN':   return 'badge-reparacion'
    case 'DESTRUIDO':       return 'badge-destruido'
    default:                return 'badge-no-construido'
  }
}

// ── Render a list of models into #armorGrid ─────────────────────────────────
function _renderArmorGrid(models) {
  const grid = document.getElementById('armorGrid')
  grid.innerHTML = ''
  models.forEach(m => {
    const card = document.createElement('div')
    card.className = 'armor-card'
    card.dataset.id = m.id
    card.innerHTML = `
      <div class="armor-sil-wrap">
        ${_armorDiagramSVG(m.id)}
        <div class="armor-scan-line"></div>
      </div>
      <div class="armor-card-name">${esc(m.name)}</div>
      <div class="armor-card-hours">${esc(m.hours)}</div>
      <div class="armor-badge ${_badgeClass(m.status)}">${esc(m.status)}</div>
      <div class="armor-card-hint">${esc((m.innovaciones || '').slice(0, 90))}</div>`
    card.addEventListener('click', () => _openDetail(m))
    grid.appendChild(card)
  })
}

// ── Detail panel open / close ───────────────────────────────────────────────
const armorDetailView = document.getElementById('armorDetailView')
