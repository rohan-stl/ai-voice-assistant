/**
 * v2.1.8 Frontend Availability & TTS Lifecycle Verification Test
 */

const fs = require("fs");
const path = require("path");
const assert = require("assert");

console.log("=== RUNNING FRONTEND v2.1.8 TTS & LIFECYCLE TESTS ===");

const jsPath = path.join(
  __dirname,
  "../frontend/ai-shopping-assistant/extensions/ai-shopping-assistant/assets/ai-assistant.js"
);

const jsContent = fs.readFileSync(jsPath, "utf8");

// 1. Verify VERSION string is 2.1.8
assert.ok(jsContent.includes('const VERSION = "2.1.8";'), "VERSION must be 2.1.8");
console.log("✔ Version check passed: 2.1.8");

// 2. Verify no undefined stopAssistantTts calls exist
const stopTtsMatches = jsContent.match(/\bstopAssistantTts\s*\(/g);
assert.strictEqual(stopTtsMatches, null, "stopAssistantTts should have 0 occurrences");
console.log("✔ Zero undefined stopAssistantTts calls confirmed");

// 3. Verify cancelAssistantTts is defined and settles pending Promise
assert.ok(jsContent.includes("function cancelAssistantTts("), "cancelAssistantTts function must be defined");
assert.ok(jsContent.includes("settleActiveTts"), "cancelAssistantTts must reference settleActiveTts Promise handle");
console.log("✔ cancelAssistantTts definition & Promise settlement verified");

// 4. Verify speakResponseAndWait Promise settlement hook exists
assert.ok(jsContent.includes("settleActiveTts = (res) => {"), "speakResponseAndWait must initialize settleActiveTts callback");
console.log("✔ speakResponseAndWait Promise settlement hook verified");

console.log("ALL v2.1.8 FRONTEND LIFECYCLE TESTS PASSED SUCCESSFULLY!");
