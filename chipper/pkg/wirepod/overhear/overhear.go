// Package overhear lets the agent ask Vector to just listen for a few seconds (no wake
// word) and get back what was said, without it being treated as a request to him.
//
// /api-sdk/overhear?serial=ESN&seconds=10 arms a capture for that robot and starts a
// wake-word-less listen (the "knowledge_question" app intent, as follow-ups do). The next
// knowledge-graph stream from that robot is then read for the full N seconds (end-of-speech
// detection ignored), transcribed locally (Vosk + Whisper), and passed on to the LLM
// endpoint as "[overheard] <text>", which the agent recognises as a capture, not a turn.
// Nothing is saved.
package overhear

import (
	"sync"
	"time"
)

const Prefix = "[overheard] "

type capture struct {
	seconds float64
	expires time.Time
}

var (
	mu      sync.Mutex
	pending = map[string]capture{}
)

// Arm makes the next knowledge-graph stream from esn (starting within ttl) a capture.
func Arm(esn string, seconds float64, ttl time.Duration) {
	mu.Lock()
	defer mu.Unlock()
	pending[esn] = capture{seconds: seconds, expires: time.Now().Add(ttl)}
}

// Take returns the armed capture length for esn and disarms it.
func Take(esn string) (float64, bool) {
	mu.Lock()
	defer mu.Unlock()
	c, ok := pending[esn]
	delete(pending, esn)
	if !ok || time.Now().After(c.expires) {
		return 0, false
	}
	return c.seconds, true
}
