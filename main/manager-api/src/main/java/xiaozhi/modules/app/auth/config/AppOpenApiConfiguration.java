package xiaozhi.modules.app.auth.config;

import io.swagger.v3.oas.models.security.SecurityScheme;
import org.springdoc.core.models.GroupedOpenApi;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
public class AppOpenApiConfiguration {
    @Bean
    public GroupedOpenApi miniappOpenApi() {
        return GroupedOpenApi.builder().group("miniapp").pathsToMatch("/app/**")
                .addOpenApiCustomizer(api -> {
                    if (api.getComponents() == null) api.setComponents(new io.swagger.v3.oas.models.Components());
                    api.getComponents().addSecuritySchemes("miniappBearer", new SecurityScheme()
                            .type(SecurityScheme.Type.HTTP).scheme("bearer")
                            .description("小程序本人业务凭据；不接受管理或设备凭据"));
                }).build();
    }
}
