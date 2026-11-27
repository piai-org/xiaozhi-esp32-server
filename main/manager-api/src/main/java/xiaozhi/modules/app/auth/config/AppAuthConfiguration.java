package xiaozhi.modules.app.auth.config;

import java.time.Clock;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
@EnableConfigurationProperties(AppAuthProperties.class)
public class AppAuthConfiguration {
    @Bean("appAuthClock")
    public Clock appAuthClock() { return Clock.systemUTC(); }
}
