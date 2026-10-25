// Majlis Party: the ten required modes. `playable` is true only once a complete, server-backed
// loop exists in this build. Everything else is shown as "Not playable yet" and cannot be chosen.
export const MODES = [
  { id: 'who_am_i', title: 'Who Am I?', ar: 'من أنا؟', blurb: 'Everyone sees your character except you. Ask yes/no questions.', playable: true },
  { id: 'feud', title: 'Majlis Feud', ar: 'تحدي المجلس', blurb: 'Buzz in and find the house answers on the board.', playable: true },
  { id: 'impostor', title: 'Impostor', ar: 'الدخيل', blurb: 'Everyone gets the same word except the impostor. Discuss, vote, catch them.', playable: true },
  { id: 'draw_guess', title: 'Draw & Guess', ar: 'ارسم وخمّن', playable: false },
  { id: 'drawing_chain', title: 'Drawing Chain', ar: 'سلسلة الرسم', playable: false },
  { id: 'bluff', title: 'Bluff', ar: 'الخداع', playable: false },
  { id: 'werewolf', title: 'Werewolf', ar: 'المستذئب', playable: false },
  { id: 'taboo', title: 'Taboo', ar: 'الممنوع', playable: false },
  { id: 'wavelength', title: 'Wavelength-style', ar: 'نفس الموجة', playable: false },
  { id: 'escape_room', title: 'Escape Room', ar: 'غرفة الهروب', playable: false },
];

export function modeInfo(id) {
  return MODES.find(m => m.id === id) || { id, title: id || 'No game chosen', playable: false };
}
