package xiaozhi.modules.app.auth.session;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.HexFormat;

public final class AppTokens {
    private static final SecureRandom RANDOM = new SecureRandom();
    private AppTokens() { }
    public static String generate() {
        byte[] bytes = new byte[32];
        RANDOM.nextBytes(bytes);
        return "app_" + Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }
    public static boolean validFormat(String value) {
        return value != null && value.matches("app_[A-Za-z0-9_-]{43}");
    }
    public static String digest(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException("SHA-256 unavailable");
        }
    }
}
