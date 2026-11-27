package xiaozhi.modules.app.security;

/** Request-scoped business identity; never installed into the management Shiro Subject. */
public record AppPrincipal(String userId, String sessionId) {
    public static final String REQUEST_ATTRIBUTE = AppPrincipal.class.getName();
}
