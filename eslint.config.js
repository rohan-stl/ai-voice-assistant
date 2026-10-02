module.exports = [
  {
    files: ["**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: {
        window: "readonly",
        document: "readonly",
        console: "readonly",
        fetch: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        localStorage: "readonly",
        CustomEvent: "readonly",
        AbortController: "readonly",
        Promise: "readonly",
        Set: "readonly",
        Object: "readonly",
        Array: "readonly",
        String: "readonly",
        Number: "readonly",
        parseInt: "readonly",
        parseFloat: "readonly",
        isNaN: "readonly",
        Math: "readonly",
        Date: "readonly",
        JSON: "readonly",
        Error: "readonly",
        SpeechRecognition: "readonly",
        webkitSpeechRecognition: "readonly",
        Audio: "readonly",
        SpeechSynthesisUtterance: "readonly",
        requestAnimationFrame: "readonly",
        navigator: "readonly",
        queueMicrotask: "readonly"
      }
    },
    rules: {
      "no-undef": "error"
    }
  }
];
