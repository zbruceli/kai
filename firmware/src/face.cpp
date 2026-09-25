#include "face.h"

#include <vector>

namespace face {
namespace {

constexpr int W = 240, H = 135;
constexpr uint32_t TICK_MS = 250;     // sprite animation step; also the frame interval when nothing moves
constexpr uint32_t ANIM_FRAME_MS = 50;  // while the mouth or sound waves follow the audio

// ---- theme: pastel -----------------------------------------------------------

struct Theme {
  uint16_t bg, line, body, eye, hl, cheek, accent, tongue, fx, iconOn, iconOff, text, textSoft, thumb;
};
Theme T;

// ---- sprite ------------------------------------------------------------------
// 20x18 grid, stored as the left half and mirrored. '#' outline, 'o' body.

constexpr int SPR_W = 20, SPR_H = 18;
const char* const HALF[SPR_H] = {
    "..........", "..........", "..........", "..........", "......####", "....##oooo",
    "...#oooooo", "..#ooooooo", "..#ooooooo", ".#oooooooo", ".#oooooooo", ".#oooooooo",
    ".#oooooooo", ".#oooooooo", "..#ooooooo", "...##ooooo", ".....#####", ".....##...",
};
char body[SPR_H][SPR_W + 1];

// Features: 'd' eye/ink, 'w' highlight, 'r' tongue, 'c' cheek.
struct Glyph {
  const char* const* rows;
  uint8_t n;
  int8_t y;   // row within the sprite
  int8_t dx;  // extra shift (looking to the side)
};
#define GLYPH(name, y, dx, ...)                          \
  const char* const name##_ROWS[] = {__VA_ARGS__};       \
  const Glyph name = {name##_ROWS, sizeof(name##_ROWS) / sizeof(char*), y, dx};

GLYPH(EYE_OPEN, 8, 0, "dw", "dd")
GLYPH(EYE_WIDE, 7, 0, "dw", "dd", "dd")
GLYPH(EYE_BLINK, 9, 0, "dd")
GLYPH(EYE_UP, 7, 1, "dw", "dd")
GLYPH(EYE_HAPPY, 8, 0, ".d.", "d.d")
GLYPH(EYE_SLEEP, 9, 0, "ddd")
GLYPH(EYE_X, 7, 0, "d.d", ".d.", "d.d")

GLYPH(MOUTH_SMILE, 11, 0, "d..d", ".dd.")
GLYPH(MOUTH_CLOSED, 12, 0, ".dd.")
GLYPH(MOUTH_OPEN, 11, 0, ".dd.", "drrd", ".dd.")
GLYPH(MOUTH_O, 11, 0, ".dd.", ".dd.")
GLYPH(MOUTH_FLAT, 12, 0, "..dd")
GLYPH(MOUTH_FROWN, 11, 0, ".dd.", "d..d")
GLYPH(MOUTH_GRIN, 11, 0, "dddd", "drrd", ".dd.")

// 7x7 icons for the bar, drawn at 2 px per dot.
const char* const ICONS[4][7] = {
    {"..ddd..", "..ddd..", "..ddd..", "d.ddd.d", ".d...d.", "..ddd..", "...d..."},  // mic
    {".......", "..dd...", ".d..dd.", "d.....d", "d.....d", ".ddddd.", "......."},  // weather
    {".......", "..ddd.d", ".d...dd", "d.d...d", ".d...dd", "..ddd.d", "......."},  // fishing
    {".......", "..dd...", "ddddddd", "d..d..d", "d.d.d.d", "d..d..d", "ddddddd"},  // camera
};

struct Pose {
  const Glyph* eyes = &EYE_OPEN;
  const Glyph* mouth = &MOUTH_SMILE;
  bool antennaTilt = false;
  uint16_t accent = 0;
  bool cheeks = false;
};

struct Pt {
  int8_t x, y;
};
// Sound-wave arcs to the right of Kai (mirrored for the left), in sprite grid coordinates.
const Pt WAVE_S[] = {{20, 8}, {21, 9}, {21, 10}, {20, 11}};
const Pt WAVE_L[] = {{22, 6}, {23, 7}, {24, 8}, {24, 9}, {24, 10}, {23, 11}, {22, 12}};
const Pt Z_SMALL[] = {{20, 4}, {21, 4}, {22, 4}, {21, 5}, {20, 6}, {21, 6}, {22, 6}};
const Pt Z_BIG[] = {{23, -1}, {24, -1}, {25, -1}, {26, -1}, {25, 0}, {24, 1}, {23, 2}, {24, 2}, {25, 2}, {26, 2}};

// ---- state -------------------------------------------------------------------

M5Canvas canvas(&M5.Display);

Expr expr = Expr::Offline;
float level = 0, shownLevel = 0;
String statusText;
int battery = -1;

struct Line {
  String text;
  bool fromCard;
};
Card replyCard;
bool replyHasCard = false;
String replyText;
std::vector<Line> lines;
bool linesDirty = false;
bool replyOn = false;
bool speaking = false;
int top = 0;
bool userScrolled = false;

uint32_t lastFrame = 0, nextBlink = 0, blinkUntil = 0, lastTick = 0;
bool dirty = true;        // something changed since the last frame
bool shownBlink = false;  // eyes were closed in the last frame
int walk = 0, walkDir = 1;

// ---- drawing helpers -----------------------------------------------------------

uint16_t colorFor(char c, const Pose& p) {
  switch (c) {
    case '#': return T.line;
    case 'o': return T.body;
    case 'd': return T.eye;
    case 'w': return T.hl;
    case 'r': return T.tongue;
    case 'c': return T.cheek;
    case 'a': return p.accent;
    default: return 0;
  }
}

void cell(int px, int py, int s, int cx, int cy, uint16_t color) { canvas.fillRect(px + cx * s, py + cy * s, s, s, color); }

void glyph(const Glyph& g, int px, int py, int s, int gx, const Pose& p) {
  for (int j = 0; j < g.n; j++)
    for (int i = 0; g.rows[j][i]; i++)
      if (g.rows[j][i] != '.') cell(px, py, s, gx + g.dx + i, g.y + j, colorFor(g.rows[j][i], p));
}

void points(const Pt* pts, size_t n, int px, int py, int s, uint16_t color, bool mirror = false) {
  for (size_t i = 0; i < n; i++) cell(px, py, s, mirror ? SPR_W - 1 - pts[i].x : pts[i].x, pts[i].y, color);
}
#define POINTS(arr, ...) points(arr, sizeof(arr) / sizeof(Pt), __VA_ARGS__)

// Kai with its top-left grid cell at pixel (px, py), each grid cell s x s pixels.
void drawKai(int px, int py, int s, const Pose& p) {
  for (int y = 0; y < SPR_H; y++)
    for (int x = 0; x < SPR_W; x++)
      if (body[y][x] != '.') cell(px, py, s, x, y, colorFor(body[y][x], p));

  if (p.cheeks) {
    for (int cx : {4, 5, 14, 15}) cell(px, py, s, cx, 10, T.cheek);
  }
  const int eyeW = strlen(p.eyes->rows[0]);
  glyph(*p.eyes, px, py, s, eyeW == 3 ? 5 : 6, p);
  glyph(*p.eyes, px, py, s, 12, p);
  glyph(*p.mouth, px, py, s, 8, p);

  // Antenna: a stem and a 2x2 ball in the accent color; tilting makes it wobble.
  if (p.antennaTilt) {
    cell(px, py, s, 10, 3, T.line);
    cell(px, py, s, 11, 2, T.line);
    canvas.fillRect(px + 11 * s, py, 2 * s, 2 * s, p.accent);
  } else {
    cell(px, py, s, 10, 2, T.line);
    cell(px, py, s, 10, 3, T.line);
    canvas.fillRect(px + 10 * s, py, 2 * s, 2 * s, p.accent);
  }
}

void drawIcon(Icon icon, int x0, int y0, uint16_t color) {
  const char* const* rows = ICONS[int(icon)];
  for (int j = 0; j < 7; j++)
    for (int i = 0; i < 7; i++)
      if (rows[j][i] == 'd') canvas.fillRect(x0 + i * 2, y0 + j * 2, 2, 2, color);
}

// ---- face view -------------------------------------------------------------------

void drawFace(uint32_t now, uint32_t tick, bool newTick) {
  constexpr int S = 5;
  Pose p;
  p.accent = T.accent;
  int bob = tick % 2;
  Icon lit = Icon::None;

  if (expr == Expr::Idle && newTick && tick % 3 == 0) {
    walk += walkDir;
    if (abs(walk) >= 8 || esp_random() % 100 < 15) walkDir = -walkDir;
  } else if (expr != Expr::Idle && newTick && walk != 0) {
    walk += walk > 0 ? -1 : 1;  // stroll back to the middle
  }

  switch (expr) {
    case Expr::Idle:
      p.antennaTilt = (tick >> 1) % 2;
      if (now < blinkUntil) p.eyes = &EYE_BLINK;
      break;
    case Expr::Listening:
      p.eyes = &EYE_WIDE;
      p.mouth = &MOUTH_O;
      p.antennaTilt = tick % 2;
      p.accent = tick % 2 ? T.accent : T.hl;
      bob = 0;
      lit = Icon::Mic;
      break;
    case Expr::Thinking:
      p.eyes = &EYE_UP;
      p.mouth = &MOUTH_FLAT;
      bob = 0;
      break;
    case Expr::Sleeping:
      p.eyes = &EYE_SLEEP;
      p.mouth = &MOUTH_CLOSED;
      bob = (tick >> 2) % 2;
      break;
    case Expr::Offline:
      p.eyes = &EYE_X;
      p.mouth = &MOUTH_FROWN;
      p.accent = T.iconOff;
      bob = 0;
      break;
  }

  // Icon bar
  for (int k = 0; k < 4; k++) drawIcon(Icon(k), 40 + k * 44, 3, Icon(k) == lit ? T.iconOn : T.iconOff);
  if (battery >= 0) {
    canvas.setFont(&fonts::Font0);
    canvas.setTextDatum(top_right);
    canvas.setTextColor(battery <= 15 ? T.tongue : T.textSoft);
    canvas.drawString(String(battery) + "%", W - 3, 3);
  }
  // Ground
  for (int x = 2; x < W / S - 2; x += 2) canvas.fillRect(x * S, 23 * S, S, S, T.iconOff);

  const int px = (14 + walk) * S, py = (5 + bob) * S;
  drawKai(px, py, S, p);

  switch (expr) {
    case Expr::Listening:  // waves grow with the voice
      if (shownLevel > 0.08f) {
        POINTS(WAVE_S, px, py, S, T.fx);
        POINTS(WAVE_S, px, py, S, T.fx, true);
      }
      if (shownLevel > 0.35f) {
        POINTS(WAVE_L, px, py, S, T.fx);
        POINTS(WAVE_L, px, py, S, T.fx, true);
      }
      break;
    case Expr::Thinking: {  // thought bubble fills in
      int n = tick % 5;
      if (n > 0) cell(px, py, S, 19, 3, T.fx);
      if (n > 1) canvas.fillRect(px + 21 * S, py + 1 * S, 2 * S, 2 * S, T.fx);
      if (n > 2) canvas.fillRect(px + 24 * S, py - 2 * S, 3 * S, 3 * S, T.fx);
      break;
    }
    case Expr::Sleeping:
      POINTS(Z_SMALL, px, py, S, T.fx);
      if ((tick >> 2) % 2 == 0) POINTS(Z_BIG, px, py, S, T.fx);
      break;
    case Expr::Offline: {  // sweat drop sliding down
      int d = (tick % 6) >> 1;
      const Pt drop[] = {{16, int8_t(5 + d)}, {15, int8_t(6 + d)}, {16, int8_t(6 + d)}, {15, int8_t(7 + d)}, {16, int8_t(7 + d)}};
      POINTS(drop, px, py, S, T.accent);
      break;
    }
    default:
      break;
  }

  if (statusText.length()) {
    canvas.setFont(&fonts::Font2);
    canvas.setTextColor(T.textSoft);
    canvas.setTextDatum(bottom_center);
    canvas.drawString(statusText, W / 2, H - 1);
  }
}

// ---- reply view ------------------------------------------------------------------

constexpr int HEADER_H = 38;
constexpr int LINE_H = 18;
constexpr int BODY_Y = HEADER_H + 3;
constexpr int VISIBLE_LINES = (H - BODY_Y) / LINE_H;  // 5
constexpr int TEXT_X = 6;
constexpr int TEXT_W = W - TEXT_X - 10;               // room for the scrollbar
constexpr int TITLE_X = 50;

void wrapInto(const String& text, bool fromCard) {
  String line;
  int start = 0;
  const int len = text.length();
  while (start < len) {
    int end = text.indexOf(' ', start);
    if (end < 0) end = len;
    String word = text.substring(start, end);
    start = end + 1;
    if (word.isEmpty()) continue;
    String candidate = line.isEmpty() ? word : line + " " + word;
    if (canvas.textWidth(candidate) <= TEXT_W) {
      line = candidate;
      continue;
    }
    if (!line.isEmpty()) lines.push_back({line, fromCard});
    while (canvas.textWidth(word) > TEXT_W) {  // hard-break words wider than the screen
      int cut = word.length() - 1;
      while (cut > 1 && canvas.textWidth(word.substring(0, cut)) > TEXT_W) cut--;
      lines.push_back({word.substring(0, cut), fromCard});
      word = word.substring(cut);
    }
    line = word;
  }
  if (!line.isEmpty()) lines.push_back({line, fromCard});
}

int maxTop() { return max(0, int(lines.size()) - VISIBLE_LINES); }

void rebuildLines() {
  canvas.setFont(&fonts::Font2);
  lines.clear();
  if (replyHasCard) {
    for (size_t i = 0; i < replyCard.count; i++) wrapInto(replyCard.lines[i], true);
  }
  wrapInto(replyText, false);
  linesDirty = false;
  top = (speaking && !userScrolled) ? maxTop() : min(top, maxTop());
}

void drawReply(uint32_t now, uint32_t tick) {
  if (linesDirty) rebuildLines();

  Pose p;
  p.accent = T.accent;
  p.antennaTilt = (tick >> 1) % 2;
  if (replyHasCard && replyCard.happy) {
    p.eyes = &EYE_HAPPY;
    p.mouth = &MOUTH_GRIN;
    p.cheeks = true;
  } else if (now < blinkUntil) {
    p.eyes = &EYE_BLINK;
  }
  if (speaking) p.mouth = shownLevel > 0.2f ? &MOUTH_OPEN : &MOUTH_CLOSED;
  drawKai(4, 1, 2, p);

  String title = replyHasCard && replyCard.title.length() ? replyCard.title : String("Kai");
  const bool hasIcon = replyHasCard && replyCard.icon != Icon::None;
  const int titleMax = W - TITLE_X - (hasIcon ? 22 : 4);
  canvas.setFont(&fonts::FreeSansBold9pt7b);
  while (title.length() > 1 && canvas.textWidth(title) > titleMax) title.remove(title.length() - 1);
  canvas.setTextDatum(middle_left);
  canvas.setTextColor(T.text);
  canvas.drawString(title, TITLE_X, HEADER_H / 2);
  if (hasIcon) drawIcon(replyCard.icon, W - 18, HEADER_H / 2 - 7, T.iconOn);
  canvas.drawFastHLine(4, HEADER_H, W - 8, T.iconOff);

  canvas.setFont(&fonts::Font2);
  canvas.setTextDatum(top_left);
  for (int i = 0; i < VISIBLE_LINES && top + i < int(lines.size()); i++) {
    const Line& l = lines[top + i];
    canvas.setTextColor(l.fromCard ? T.text : T.textSoft);
    canvas.drawString(l.text, TEXT_X, BODY_Y + i * LINE_H);
  }

  if (int(lines.size()) > VISIBLE_LINES) {
    const int trackY = BODY_Y, trackH = H - BODY_Y - 3;
    const int thumbH = max(10, trackH * VISIBLE_LINES / int(lines.size()));
    const int thumbY = trackY + (trackH - thumbH) * top / max(1, maxTop());
    canvas.fillRoundRect(W - 5, trackY, 3, trackH, 1, T.iconOff);
    canvas.fillRoundRect(W - 5, thumbY, 3, thumbH, 1, T.thumb);
  }
}

}  // namespace

void begin() {
  M5.Display.setRotation(1);
  M5.Display.setBrightness(90);  // BRIGHTNESS_AWAKE in main.cpp
  canvas.setColorDepth(16);
  canvas.createSprite(W, H);

  for (int y = 0; y < SPR_H; y++) {
    for (int x = 0; x < SPR_W / 2; x++) body[y][x] = body[y][SPR_W - 1 - x] = HALF[y][x];
    body[y][SPR_W] = 0;
  }

  auto c = [](uint32_t rgb) { return canvas.color565(rgb >> 16, (rgb >> 8) & 0xff, rgb & 0xff); };
#ifdef KAI_LIGHT_THEME
  T = {  // pastel on cream
      .bg = c(0xfff3e3), .line = c(0x4b3a63), .body = c(0xcdb9ff), .eye = c(0x4b3a63),
      .hl = c(0xffffff), .cheek = c(0xff9fb8), .accent = c(0xffc94d), .tongue = c(0xff7d9e),
      .fx = c(0x4b3a63), .iconOn = c(0x4b3a63), .iconOff = c(0xeadcc9), .text = c(0x4b3a63),
      .textSoft = c(0x7d6f95), .thumb = c(0x9d86e9),
  };
#else
  T = {  // pastel on deep plum (default)
      .bg = c(0x16111f), .line = c(0x6b55a3), .body = c(0xcdb9ff), .eye = c(0x2a1f3d),
      .hl = c(0xffffff), .cheek = c(0xff9fb8), .accent = c(0xffc94d), .tongue = c(0xff7d9e),
      .fx = c(0xcdb9ff), .iconOn = c(0xcdb9ff), .iconOff = c(0x2e2540), .text = c(0xefe6ff),
      .textSoft = c(0xa99bc4), .thumb = c(0x9d86e9),
  };
#endif
}

void setExpr(Expr e) {
  dirty |= e != expr;
  expr = e;
}
void setLevel(float l) { level = l; }
void setStatusText(const String& text) {
  dirty |= text != statusText;
  statusText = text;
}
void setBattery(int percent) {
  dirty |= percent != battery;
  battery = percent;
}

void clearReply() {
  replyHasCard = false;
  replyCard = Card{};
  replyText = "";
  lines.clear();
  top = 0;
  userScrolled = false;
  speaking = false;
  dirty = true;
}

void setReplyCard(const Card& card) {
  replyCard = card;
  replyHasCard = true;
  linesDirty = dirty = true;
}

void appendReplyText(const String& text) {
  replyText += text;
  linesDirty = dirty = true;
}

bool hasReply() { return replyHasCard || replyText.length() > 0; }
void showReply(bool on) {
  dirty |= on != replyOn;
  replyOn = on;
}
bool replyVisible() { return replyOn; }
void setSpeaking(bool s) {
  dirty |= s != speaking;
  speaking = s;
}

void scrollReply() {
  if (linesDirty) rebuildLines();
  userScrolled = true;
  top = (top >= maxTop()) ? 0 : min(top + VISIBLE_LINES - 1, maxTop());
  dirty = true;
}

void replyFinished() {
  speaking = false;
  if (!userScrolled) top = 0;
  dirty = true;
}

void render() {
  // Power: draw only when something changed, a blink starts or ends, or the animation needs a new
  // frame (20 fps while following audio, 4 fps otherwise). A full frame costs ~15 ms of CPU and SPI.
  uint32_t now = millis();
  bool blinkEdge = false;
  if (now >= nextBlink) {
    blinkUntil = now + 150;
    nextBlink = now + 2500 + esp_random() % 3500;
    blinkEdge = true;
  }
  blinkEdge |= (now < blinkUntil) != shownBlink;
  const bool animating = replyOn ? speaking : expr == Expr::Listening;
  const uint32_t interval = animating ? ANIM_FRAME_MS : TICK_MS;
  if (!dirty && !blinkEdge && now - lastFrame < interval) return;
  lastFrame = now;
  dirty = false;
  shownBlink = now < blinkUntil;

  const uint32_t tick = now / TICK_MS;
  const bool newTick = tick != lastTick;
  lastTick = tick;
  shownLevel += (level - shownLevel) * 0.5f;

  canvas.fillSprite(T.bg);
  if (replyOn) {
    drawReply(now, tick);
  } else {
    drawFace(now, tick, newTick);
  }
  canvas.pushSprite(0, 0);
}

}  // namespace face
