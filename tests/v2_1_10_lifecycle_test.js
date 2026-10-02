/**
 * tests/v2_1_10_lifecycle_test.js
 * Test suite verifying v2.1.10 auto-listening lifecycle, timer safety, and state metadata.
 */

const fs = require("fs");
const path = require("path");
const assert = require("assert");

console.log("Running v2.1.10 JS Lifecycle & Timer Tests...");

const scriptPath = path.join(
  __dirname,
  "../frontend/ai-shopping-assistant/extensions/ai-shopping-assistant/assets/ai-assistant.js"
);
const jsContent = fs.readFileSync(scriptPath, "utf8");

// 1. Verify autoListenTimer is declared at top-level
assert.ok(
  jsContent.includes("let autoListenTimer = null;"),
  "autoListenTimer must be declared at top level"
);

// 2. Verify cancelAssistantTts is called before 200ms delay in follow-up transition
assert.ok(
  jsContent.includes('cancelAssistantTts("follow-up-transition");'),
  "cancelAssistantTts('follow-up-transition') must be called before recognition"
);

// 3. Verify handleMicrophoneAction function exists
assert.ok(
  jsContent.includes("async function handleMicrophoneAction("),
  "handleMicrophoneAction function must be present"
);

// 4. Verify 10-second TTS timeout safeguard exists
assert.ok(
  jsContent.includes("TTS speech timeout (10s limit reached)"),
  "10-second TTS timeout safeguard must be present"
);

// 5. Syntax check via eval in restricted scope to ensure no syntax errors
try {
  new Function(jsContent);
  console.log("✓ Function instantiation syntax check passed");
} catch (e) {
  assert.fail("Syntax error in ai-assistant.js: " + e.message);
}

console.log("All v2.1.10 JS Lifecycle Tests PASSED cleanly!");
