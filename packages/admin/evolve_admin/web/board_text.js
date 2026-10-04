// board_text.js — the ONE place the Board and the Stack turn bot-written
// values into something a person reads (brief:
// board-sheet-renders-time-text-and-price-is-warmed). Served at
// /board/board-text.js and loaded by board.html and stack.html before their
// own script, so the tile, the sheet and the Stack card face cannot drift.
//
// WHICH ZONE IS AUTHORITATIVE: the viewer's device. An instant (a `when` with
// Z or an offset) is shown in the zone the phone is in right now — the phone
// travels, the pod does not, and the person holding it is the one who has to
// be there. When the value was written with an offset that differs from the
// viewer's, the text says "your time (PDT)" so a converted hour is never
// mistaken for the source's. A date-time with NO offset names no instant: it
// is shown as written and labelled "time zone not stated". A bare date is a
// calendar day and is never shifted.
//
// Never the HTML parser: every piece of bot text reaches the DOM through
// textContent or a node built by createElement.
(function (root) {
  "use strict";
  var UNKNOWN = "time unknown";
  var DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;
  var DATE_TIME = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/i;

  function parse(s) {
    if (typeof s !== "string") return null;
    s = s.trim();
    var m = DATE_ONLY.exec(s) || DATE_TIME.exec(s);
    if (!m) return null;
    var wall = Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0));
    var d = new Date(wall);
    // Reject what Date.UTC silently rolls over (Feb 31, 25:00).
    if (d.getUTCMonth() !== +m[2] - 1 || d.getUTCDate() !== +m[3] ||
        d.getUTCHours() !== +(m[4] || 0) || d.getUTCMinutes() !== +(m[5] || 0)) return null;
    if (m[4] === undefined) return { kind: "date", ms: wall };
    if (!m[7]) return { kind: "floating", ms: wall };
    var off = 0;
    if (m[7].toUpperCase() !== "Z") {
      var digits = m[7].slice(1).replace(":", "");
      off = (m[7][0] === "-" ? -1 : 1) * (+digits.slice(0, 2) * 60 + +digits.slice(2));
    }
    return { kind: "instant", ms: wall - off * 60000, offset: m[7].toUpperCase() === "Z" ? null : off };
  }

  function fmt(opts, zone) {
    if (zone) opts.timeZone = zone;
    return new Intl.DateTimeFormat(undefined, opts);
  }

  // Minutes east of UTC for the viewer's zone at `ms`, or null if this
  // browser cannot say (then the zone is always labelled — the safe side).
  function viewerOffset(ms) {
    try {
      var parts = fmt({ timeZoneName: "longOffset" }).formatToParts(new Date(ms));
      var name = parts.filter(function (p) { return p.type === "timeZoneName"; })[0].value;
      var m = /GMT(?:([+-])(\d{2}):?(\d{2}))?/.exec(name);
      return !m[1] ? 0 : (m[1] === "-" ? -1 : 1) * (+m[2] * 60 + +m[3]);
    } catch (_e) { return null; }
  }

  function zoneShort(ms) {
    try {
      return fmt({ timeZoneName: "short" }).formatToParts(new Date(ms))
        .filter(function (p) { return p.type === "timeZoneName"; })[0].value;
    } catch (_e) { return "local"; }
  }

  // `when` ({start, end?} or a bare ISO string) → {text, raw, known}.
  function formatWhen(value) {
    var raw = typeof value === "string" ? value : JSON.stringify(value);
    var unknown = { text: UNKNOWN, raw: raw, known: false };
    try {
      var startRaw = typeof value === "string" ? value : value && value.start;
      var endRaw = value && typeof value === "object" ? value.end : undefined;
      var a = parse(startRaw);
      var b = endRaw === undefined || endRaw === null ? null : parse(endRaw);
      if (!a || (endRaw != null && !b)) return unknown;
      if (b && (b.kind === "instant") !== (a.kind === "instant")) return unknown;
      if (b && b.ms < a.ms) return unknown;
      var zone = a.kind === "instant" ? undefined : "UTC";
      var day = fmt({ weekday: "short", month: "short", day: "numeric" }, zone);
      var time = fmt({ hour: "numeric", minute: "2-digit" }, zone);
      var A = new Date(a.ms), B = b && new Date(b.ms), text;
      if (a.kind === "date" || (b && b.kind === "date")) {
        text = day.format(A) + (B && day.format(B) !== day.format(A) ? " – " + day.format(B) : "");
      } else if (!B) {
        text = day.format(A) + ", " + time.format(A);
      } else if (day.format(A) === day.format(B)) {
        text = day.format(A) + ", " + (time.formatRange ? time.formatRange(A, B)
          : time.format(A) + " – " + time.format(B));
      } else {
        text = day.format(A) + ", " + time.format(A) + " – " + day.format(B) + ", " + time.format(B);
      }
      if (a.kind === "floating") text += " (time zone not stated)";
      else if (a.kind === "instant" && a.offset !== null && a.offset !== viewerOffset(a.ms)) {
        text += " your time (" + zoneShort(a.ms) + ")";
      }
      return { text: text, raw: raw, known: true };
    } catch (_e) {
      return unknown;  // never a throw: that blanks the card
    }
  }

  // A span with the rendered `when`, the raw value on hover.
  function whenNode(doc, value) {
    var w = formatWhen(value);
    var span = doc.createElement("span");
    span.textContent = w.text;
    span.title = w.raw;
    return span;
  }

  var URL_RE = /https?:\/\/[^\s<>()[\]"'`]+/i;
  var LIST_RE = /^\s*(?:[-*]|\d+[.)])\s+/;

  function textSpan(doc, text, tag) {
    var n = doc.createElement(tag || "span");
    n.textContent = text;
    return n;
  }

  // Inline pass over one line: **bold** and bare http(s) URLs; all else literal.
  function inline(doc, parent, line) {
    var parts = line.split(/\*\*([^*]+)\*\*/);
    parts.forEach(function (part, i) {
      if (!part) return;
      var host = i % 2 ? parent.appendChild(doc.createElement("strong")) : parent;
      var rest = part, m;
      while ((m = URL_RE.exec(rest))) {
        var url = m[0].replace(/[.,;:!?]+$/, "");
        if (m.index) host.appendChild(textSpan(doc, rest.slice(0, m.index)));
        var a = doc.createElement("a");
        a.href = url;
        a.textContent = url;  // the URL itself — never a bot-supplied label
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        host.appendChild(a);
        rest = rest.slice(m.index + url.length);
      }
      if (rest) host.appendChild(textSpan(doc, rest));
    });
  }

  // Bot-written text → a <div> of lines: **bold**, list markers kept with a
  // break per item, http(s) links as anchors whose text is their URL.
  function renderBotText(doc, text, cls) {
    var box = doc.createElement("div");
    if (cls) box.className = cls;
    String(text == null ? "" : text).split(/\r?\n/).forEach(function (line, i) {
      if (i) box.appendChild(doc.createElement("br"));
      inline(doc, box, LIST_RE.test(line) ? line.replace(/^\s+/, "") : line);
    });
    return box;
  }

  root.BoardText = { formatWhen: formatWhen, whenNode: whenNode, renderBotText: renderBotText, UNKNOWN: UNKNOWN };
})(window);
