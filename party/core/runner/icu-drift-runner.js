// Evidence probe (not shipped): which code points does the host ICU normalize
// differently from the pinned Unicode-13 core, and what does the core return.
var MajlisTraceRunner = {
  runLine: function () {
    var U = MajlisCore.unicode, rows = [];
    function cps(s) { var o = []; for (var c of s) o.push(c.codePointAt(0)); return o; }
    for (var cp = 0; cp < 0x110000; cp++) {
      if (cp >= 0xD800 && cp <= 0xDFFF) continue;
      var ch = String.fromCodePoint(cp);
      var hostK = ch.normalize("NFKC"), coreK = U.nfkc(ch);
      var hostC = ch.normalize("NFC"), coreC = U.nfc(ch);
      var hostKey = hostK.toLowerCase().split(/\s+/).filter(Boolean).join(" ");
      if (hostK !== coreK || hostC !== coreC || hostKey !== U.matchKey(ch)) {
        rows.push({ cp: cp, hostNFKC: cps(hostK), coreNFKC: cps(coreK), hostNFC: cps(hostC), coreNFC: cps(coreC),
                    coreMatchKey: cps(U.matchKey(ch)), coreClean24: U.cleanText(ch, 24) === null ? null : "kept" });
      }
    }
    return JSON.stringify(rows);
  }
};
