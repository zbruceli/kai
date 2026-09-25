// Kai's look: an original Tamagotchi-style pixel pet on the 240x135 display, drawn into an off-screen
// canvas to avoid flicker. Pastel on deep plum by default; build with -DKAI_LIGHT_THEME for cream.
//
// Two views:
//   face  - Kai at 5x pixel size with an icon bar, wandering, blinking and reacting to its state
//   reply - Kai at 2x in a header, then the tool card (if any) and a live transcript of what Kai says,
//           scrollable with the side button
#pragma once
#include <M5Unified.h>

enum class Expr { Offline, Idle, Listening, Thinking, Sleeping };

// Icon bar slots, matching the relay's card "icon" names.
enum class Icon : int8_t { None = -1, Mic = 0, Weather, Fishing, Camera };

constexpr size_t CARD_LINES = 5;

struct Card {
  String title;
  String lines[CARD_LINES];
  size_t count = 0;
  Icon icon = Icon::None;
  bool happy = false;
};

namespace face {

void begin();
void render();  // call every loop; throttles itself to ~30 fps

// Face view
void setExpr(Expr e);
void setLevel(float level);              // drives the sound waves while listening and the mouth while speaking
void setStatusText(const String& text);  // small hint under Kai (e.g. "connecting wifi")
void setBattery(int percent);

// Reply view
void clearReply();
void setReplyCard(const Card& card);
void appendReplyText(const String& text);
bool hasReply();
void showReply(bool on);
bool replyVisible();
void setSpeaking(bool speaking);
void scrollReply();    // page down; wraps back to the top at the end
void replyFinished();  // Kai stopped talking: jump back to the top unless the user has been scrolling

}  // namespace face
