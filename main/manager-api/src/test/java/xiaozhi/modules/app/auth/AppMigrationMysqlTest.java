package xiaozhi.modules.app.auth;

import static org.junit.jupiter.api.Assertions.*;
import java.util.List;
import javax.sql.DataSource;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeLogParameters;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.parser.ChangeLogParserFactory;
import liquibase.integration.spring.SpringResourceAccessor;
import org.springframework.core.io.DefaultResourceLoader;

@EnabledIfEnvironmentVariable(named = "MINIAPP_TEST_JDBC_URL", matches = ".+")
class AppMigrationMysqlTest {
    private static final String MASTER = "db/changelog/db.changelog-master.yaml";
    private static final String CHANGESET = "202610211200";
    private DataSource resetSchema(String name) {
        if (!List.of("xiaozhi_miniapp_migration_fresh", "xiaozhi_miniapp_migration_upgrade").contains(name))
            throw new IllegalArgumentException("Isolated migration schema required");
        JdbcTemplate admin = new JdbcTemplate(MiniappTestDatabase.connect());
        admin.execute("DROP DATABASE IF EXISTS " + name);
        admin.execute("CREATE DATABASE " + name + " CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci");
        return new DriverManagerDataSource(System.getenv("MINIAPP_TEST_JDBC_URL")
                .replace("/xiaozhi_miniapp_test", "/" + name), "root", "isolated-test-only");
    }
    private Liquibase migrator(DataSource dataSource, boolean baselineOnly) throws Exception {
        var resources = new SpringResourceAccessor(new DefaultResourceLoader());
        var database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(dataSource.getConnection()));
        var changelog = ChangeLogParserFactory.getInstance().getParser(MASTER, resources)
                .parse(MASTER, new ChangeLogParameters(database), resources);
        if (baselineOnly) assertTrue(changelog.getChangeSets().removeIf(set -> CHANGESET.equals(set.getId())));
        return new Liquibase(changelog, resources, database);
    }
    @Test void emptyDatabaseSupportsFullHistoricalMigrationAndSecondRunIsNoOp() throws Exception {
        DataSource source = resetSchema("xiaozhi_miniapp_migration_fresh");
        try (Liquibase migration = migrator(source, false)) {
            migration.update(new Contexts(), new LabelExpression());
        }
        JdbcTemplate jdbc = new JdbcTemplate(source);
        assertEquals(3, jdbc.queryForObject("SELECT COUNT(*) FROM information_schema.tables "
                + "WHERE table_schema=DATABASE() AND table_name IN ('app_user','app_wechat_identity','app_session')", Integer.class));
        int count = jdbc.queryForObject("SELECT COUNT(*) FROM DATABASECHANGELOG", Integer.class);
        try (Liquibase second = migrator(source, false)) {
            assertTrue(second.listUnrunChangeSets(new Contexts(), new LabelExpression()).isEmpty());
            second.update(new Contexts(), new LabelExpression());
        }
        assertEquals(count, jdbc.queryForObject("SELECT COUNT(*) FROM DATABASECHANGELOG", Integer.class));
    }
    @Test void upgradeFromOldMasterExecutesOnlyNewChangesetAndPreservesManagementData() throws Exception {
        DataSource source = resetSchema("xiaozhi_miniapp_migration_upgrade");
        try (Liquibase baseline = migrator(source, true)) { baseline.update(new Contexts(), new LabelExpression()); }
        JdbcTemplate jdbc = new JdbcTemplate(source);
        jdbc.update("INSERT INTO sys_user(id,username,status,super_admin) VALUES (21,'migration-preserve-fixture',1,0)");
        List<java.util.Map<String, Object>> before = jdbc.queryForList("SELECT * FROM sys_user ORDER BY id");
        assertFalse(before.isEmpty());
        int count = jdbc.queryForObject("SELECT COUNT(*) FROM DATABASECHANGELOG", Integer.class);
        try (Liquibase upgrade = migrator(source, false)) {
            var pending = upgrade.listUnrunChangeSets(new Contexts(), new LabelExpression());
            assertEquals(1, pending.size());
            assertEquals(CHANGESET, pending.getFirst().getId());
            upgrade.update(new Contexts(), new LabelExpression());
        }
        assertEquals(count + 1, jdbc.queryForObject("SELECT COUNT(*) FROM DATABASECHANGELOG", Integer.class));
        assertEquals(before, jdbc.queryForList("SELECT * FROM sys_user ORDER BY id"));
    }
}
