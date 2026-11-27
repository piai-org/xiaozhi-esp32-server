package xiaozhi.modules.app.auth.controller;

import com.fasterxml.jackson.annotation.JsonProperty;

/** No generated toString: the raw token must not enter diagnostic logs. */
public final class AppLoginResponse {
    private final String userId;
    private final String accessToken;
    private final int expiresIn;
    private final String expiresAt;
    public AppLoginResponse(String userId, String accessToken, int expiresIn, String expiresAt) {
        this.userId = userId;
        this.accessToken = accessToken;
        this.expiresIn = expiresIn;
        this.expiresAt = expiresAt;
    }
    @JsonProperty("user_id") public String getUserId() { return userId; }
    @JsonProperty("access_token") public String getAccessToken() { return accessToken; }
    @JsonProperty("token_type") public String getTokenType() { return "Bearer"; }
    @JsonProperty("expires_in") public int getExpiresIn() { return expiresIn; }
    @JsonProperty("expires_at") public String getExpiresAt() { return expiresAt; }
}
