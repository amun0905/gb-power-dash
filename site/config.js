// Where the website reads its data. The pipeline writes this file on every update.
// Override for testing with ?data=<url> in the page address.
window.GBDASH_CONFIG = {
  DATA_URL: "https://pub-2828285bab12449a8361d29bcb8483fe.r2.dev/web/overview.json",
  REFRESH_MINUTES: 5,
};
