// Kai's face and reply screen on the 240x135 display, drawn into an off-screen canvas to avoid flicker.
//
// Two views:
//   face  - big animated eyes for idle / listening / thinking / offline
//   reply - mini face in a header, then the tool card (if any) and a live transcript of what Kai says,
//           scrollable with the side button
#pragma once
#include <M5Unified.h>

enum class Expr { Offline, Idle, Listening, Thinking };

constexpr size_t CARD_LINES = 5;

struct Card {
  String title;
  String lines[CARD_LINES];
  size_t count = 0;
};

namespace face {

void begin();
void render();  // call every loop; throttles itself to ~30 fps

// Face view
void setExpr(Expr e);
void setLevel(float level);              // mic meter while listening, mouth while speaking
void setStatusText(const String& text);  // small hint under the face (e.g. "connecting wifi")
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
