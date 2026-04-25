from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import render
from main.views import custom_ckeditor_upload
from users.views import check_domain

urlpatterns = [
    path('admin/', admin.site.urls),
		path('', include('main.urls')),
    path('users/', include('users.urls')),
    path('dashboard/', include('dashboard.urls')),
    path('affiliates/', include('affiliates.urls')),
    # path('warmup/', include('warmup.urls')),
    path('warmup/', include('new_warmup.urls')),
    path('drip-campaigns/', include('drip_campaigns.urls')),
    path('leads_data/', include('leads_data.urls')),

    path('check-domain/', check_domain, name='check_domain'),
    
		path("ckeditor5/image_upload/", custom_ckeditor_upload, name="custom_ckeditor_upload"),
    path("ckeditor5/", include('django_ckeditor_5.urls')),
]

def custom_404(request, exception):
    return render(request, "404.html", status=404)

handler404 = custom_404

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL,document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


