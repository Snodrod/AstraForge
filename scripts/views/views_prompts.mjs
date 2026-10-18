// Slots ("views") and the prompts that ask an image model for the missing views of a T-pose character.
//
// Conventions used everywhere in AstraForge:
//   front  — the character looks at the camera.
//   left   — camera on the CHARACTER'S left: the left side faces us, the character looks toward the LEFT edge of the image.
//   back   — seen from behind.
//   right  — camera on the character's right: the right side faces us, the character looks toward the RIGHT edge of the image.
//   frontLeft / frontRight — 45 degrees between front and left / front and right.
// Hunyuan3D-2mv takes front/left/back/right; the 45-degree views are for people (and MV-Adapter provides them).

export const SLOTS = ['front', 'left', 'back', 'right', 'frontLeft', 'frontRight'];
export const SHAPE_SLOTS = ['front', 'left', 'back', 'right'];
export const GENERATED_SLOTS = ['left', 'back', 'right', 'frontLeft', 'frontRight'];

// MV-Adapter azimuths (checked on a real run: azimuth 90 shows the character's LEFT side, 270 the right side).
export const SLOT_META = {
  front: { label: 'Front', azimuth: 0 },
  frontLeft: { label: 'Front-left', azimuth: 45 },
  left: { label: 'Left', azimuth: 90 },
  back: { label: 'Back', azimuth: 180 },
  right: { label: 'Right', azimuth: 270 },
  frontRight: { label: 'Front-right', azimuth: 315 },
};

const KEEP = 'Keep EVERYTHING else exactly as in the reference: the same character, the same T-pose (arms stretched straight out to the sides), '
  + 'the same proportions, the same figure size and the same position in the frame, the same camera height and distance, '
  + 'the same art style and rendering (flat-shaded low-poly if it is low-poly, painted if it is painted), the same colors and lighting. '
  + 'Plain light grey studio background, soft floor shadow. Nothing in the hands, no weapons, no extra props. No text, no watermark, no collage, exactly one figure.';

/** Prompt (English: the image model follows it best) for one missing view. `note` is the user's free-text hint. */
export function codexPrompt(slot, { hasBack = false, note = '' } = {}) {
  const refs = hasBack
    ? 'Image 1 is the FRONT view and image 2 is the BACK view of one game character in a T-pose. '
    : 'Image 1 is the FRONT view of a game character in a T-pose. ';
  const hint = note ? ` Extra details about the character: ${note.trim()}.` : '';
  const side = (name, lookEdge, nearArm, farArm) => `${refs}Create the ${name} view of exactly the same character, seen from exactly 90 degrees `
    + `to the character's ${name === 'LEFT-SIDE' ? 'left' : 'right'} (a strict orthographic side view, camera at the character's chest height, no perspective tilt). `
    + `The character's ${name === 'LEFT-SIDE' ? 'left' : 'right'} side faces the camera and the character looks toward the ${lookEdge} edge of the image. `
    + `The T-pose is kept in 3D: the arms still point straight out sideways in space, so in this view the ${nearArm} arm points straight at the camera `
    + `(foreshortened: only the shoulder, the sleeve cuff end-on and the hand are seen, overlapping the torso) and the ${farArm} arm points directly away behind the body and is hidden. `
    + 'Show the true profile: the face in profile, the nose and the brim/visor of the helmet or hat in profile, the thickness of the chest and the back, the profile of belts, pouches, '
    + 'capes and skirts as they hang, the profile of the legs and the boots. Do not draw the front view and do not rotate the character toward the camera.'
    + hint + ' ' + KEEP;
  switch (slot) {
    case 'back':
      return `${refs}Create the BACK view of exactly the same character, seen from directly behind (rotated 180 degrees around the vertical axis), `
        + 'with the same camera height and distance. Show the back of every garment and piece of gear as it really looks from behind: the back of the head, hair or helmet from behind, '
        + 'the back of the clothes, belts and straps crossing the back, anything hanging from the belt on the correct side (the character\'s own left stays on the image\'s LEFT when seen from behind), '
        + 'the back of the legs, the heels. The face is not visible.' + hint + ' ' + KEEP;
    case 'left':
      return side('LEFT-SIDE', 'LEFT', 'left', 'right');
    case 'right':
      return side('RIGHT-SIDE', 'RIGHT', 'right', 'left');
    case 'frontLeft':
      return `${refs}Create the view of the same character from 45 degrees to the character's left in front (three-quarter view, the character's left shoulder is nearer to the camera, `
        + 'the face turned toward the camera by 45 degrees), orthographic, same camera height.' + hint + ' ' + KEEP;
    case 'frontRight':
      return `${refs}Create the view of the same character from 45 degrees to the character's right in front (three-quarter view, the character's right shoulder is nearer to the camera, `
        + 'the face turned toward the camera by 45 degrees), orthographic, same camera height.' + hint + ' ' + KEEP;
    default:
      throw new Error('No generation prompt for view "' + slot + '".');
  }
}

/** Order that lets later views use earlier ones as extra references (back first, then the sides). */
export function generationOrder(slots) {
  const rank = { back: 0, left: 1, right: 2, frontLeft: 3, frontRight: 4 };
  return [...slots].sort((a, b) => (rank[a] ?? 9) - (rank[b] ?? 9));
}
