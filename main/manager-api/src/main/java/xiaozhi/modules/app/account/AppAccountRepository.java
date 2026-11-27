package xiaozhi.modules.app.account;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import javax.sql.DataSource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Explicit SQL avoids the management-user audit filler and its Shiro principal. */
@Repository
public class AppAccountRepository {
    private final JdbcTemplate jdbc;
    public AppAccountRepository(DataSource dataSource) {
        jdbc = new JdbcTemplate(dataSource);
        jdbc.setQueryTimeout(3);
    }
    public record User(String id, int status) { }
    public record Session(String id, String userId, Instant expiresAt, Instant revokedAt, int userStatus) { }
    private static LocalDateTime utc(Instant value) { return LocalDateTime.ofInstant(value, ZoneOffset.UTC); }

    public User findIdentity(String appId, String openId) {
        List<User> users = jdbc.query("SELECT u.id,u.status FROM app_user u JOIN app_wechat_identity w "
                + "ON w.user_id=u.id WHERE w.app_id=? AND w.open_id=?", (rs, row) ->
                new User(rs.getString("id"), rs.getInt("status")), appId, openId);
        return users.isEmpty() ? null : users.getFirst();
    }
    public User lockUser(String userId) {
        return jdbc.queryForObject("SELECT id,status FROM app_user WHERE id=? FOR UPDATE", (rs, row) ->
                new User(rs.getString("id"), rs.getInt("status")), userId);
    }
    public void createUser(String id, Instant now) {
        jdbc.update("INSERT INTO app_user(id,status,created_at,updated_at) VALUES (?,1,?,?)", id, utc(now), utc(now));
    }
    public void createIdentity(String id, String userId, String appId, String openId, Instant now) {
        jdbc.update("INSERT INTO app_wechat_identity(id,user_id,app_id,open_id,created_at) VALUES (?,?,?,?,?)",
                id, userId, appId, openId, utc(now));
    }
    public void createSession(String id, String userId, String digest, Instant now, Instant expiresAt) {
        jdbc.update("INSERT INTO app_session(id,user_id,token_hash,created_at,expires_at) VALUES (?,?,?,?,?)",
                id, userId, digest, utc(now), utc(expiresAt));
    }
    public Session findSession(String digest) {
        List<Session> sessions = jdbc.query("SELECT s.id,s.user_id,s.expires_at,s.revoked_at,u.status "
                + "FROM app_session s JOIN app_user u ON s.user_id=u.id WHERE s.token_hash=?", (rs, row) -> {
                    LocalDateTime revoked = rs.getObject("revoked_at", LocalDateTime.class);
                    return new Session(rs.getString("id"), rs.getString("user_id"),
                            rs.getObject("expires_at", LocalDateTime.class).toInstant(ZoneOffset.UTC),
                            revoked == null ? null : revoked.toInstant(ZoneOffset.UTC), rs.getInt("status"));
                }, digest);
        return sessions.isEmpty() ? null : sessions.getFirst();
    }
    public void revoke(String id, String userId, Instant now) {
        jdbc.update("UPDATE app_session SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                utc(now), id, userId);
    }
    public int cleanup(Instant cutoff) {
        return jdbc.update("DELETE FROM app_session WHERE expires_at<? OR revoked_at<? LIMIT 1000",
                utc(cutoff), utc(cutoff));
    }
}
