from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.contrib.auth.base_user import BaseUserManager
from django.utils import timezone
from django_resized import ResizedImageField
import uuid

from .usernames import generate_unique_username

class AppUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        if not extra_fields.get('username'):
            extra_fields['username'] = generate_unique_username(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self.create_user(email, password, **extra_fields)


def get_profile_picture_save_path(instance, filename):
    # A new name per upload, so a replaced picture is never served from a cache
    return f"user_files/{instance.id}/avatar_{uuid.uuid4().hex}.jpg"

class AppUser(AbstractBaseUser,PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    username = models.CharField(max_length=150, unique=True)
    first_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)
    
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    
    date_joined = models.DateTimeField(default=timezone.now)
    last_login = models.DateTimeField(null=True, blank=True)
    
    # Profile fields (can be updated later)
    profile_picture = models.ImageField(upload_to=get_profile_picture_save_path, blank=True, null=True)
    bio = models.TextField(max_length=500, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    
    objects = AppUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = 'Application User'
        verbose_name_plural = 'Application Users'
        constraints = [
            models.UniqueConstraint(Lower('username'), name='appuser_username_ci_unique'),
        ]

    def __str__(self):
        return f"Username: {self.username} | Email: {self.email} | User ID: {self.id}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def save(self, *args, **kwargs):
        if not self.username and self.email:
            self.username = generate_unique_username(self.email, exclude_user=self)
        super().save(*args, **kwargs)


DEFAULT_TIMEZONE = 'Asia/Dhaka'
SATURDAY = 5


def validate_timezone(name):
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError('Not a time zone the server knows.')


class UserSettings(models.Model):
    MODE_CHOICES = [
        ('personal', 'Personal'),
        ('business', 'Business'),
    ]

    user = models.OneToOneField(AppUser, on_delete=models.CASCADE, related_name='settings')

    get_auto_reports=models.BooleanField(default=False)
    # Set when the user makes the individual-or-business choice, so a
    # reinstall or a second phone skips onboarding
    onboarded_at = models.DateTimeField(null=True, blank=True)
    primary_mode = models.CharField(max_length=10, choices=MODE_CHOICES, blank=True, default='')
    # Where the user's day begins and ends: an IANA name the app reports from the
    # phone. "Today" for what falls due, and the hour of a reminder, are read in it.
    timezone = models.CharField(max_length=64, default=DEFAULT_TIMEZONE, validators=[validate_timezone])
    # The day weeks start on, Monday as 0. New recurring rules copy it.
    week_start = models.PositiveSmallIntegerField(default=SATURDAY, validators=[MaxValueValidator(6)])

    class Meta:
        verbose_name = 'User Setting'
        verbose_name_plural = 'User Settings'
    
    def __str__(self):
        return f"Settings for {self.user.username}"
