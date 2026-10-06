from django.urls import path

from . import views

urlpatterns = [
    path("", views.ApiRootView.as_view(), name="api-root"),
    path("route/", views.RoutePlanView.as_view(), name="route-plan"),
    path("route/map/", views.route_map, name="route-map"),
    path("health/", views.HealthView.as_view(), name="health"),
]
