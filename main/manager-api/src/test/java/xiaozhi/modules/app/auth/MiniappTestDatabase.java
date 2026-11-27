package xiaozhi.modules.app.auth;

import org.springframework.jdbc.datasource.DriverManagerDataSource;

/** Prevent destructive integration fixtures from ever using the deployed database. */
public final class MiniappTestDatabase {
    private MiniappTestDatabase() { }
    public static DriverManagerDataSource connect() {
        String url = System.getenv("MINIAPP_TEST_JDBC_URL");
        if (url == null || !url.matches("jdbc:mysql://test-db:3306/xiaozhi_miniapp_test(?:\\?.*)?"))
            throw new IllegalStateException("Use the isolated miniapp test container/database only");
        return new DriverManagerDataSource(url, "root", "isolated-test-only");
    }
}
