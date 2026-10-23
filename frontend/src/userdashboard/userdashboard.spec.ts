import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { AuthServ } from '../auth-serv';
import { UserDashboard } from './userdashboard';

describe('UserDashboard', () => {
  let component: UserDashboard;
  let fixture: ComponentFixture<UserDashboard>;

  const authStub = {
    getMe: () =>
      of({
        id: 1,
        name: 'Test User',
        email: 'user@example.com',
        role: 'user' as const,
        email_verified: true
      }),
    clearSession: () => undefined,
    logout: () => undefined
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [UserDashboard],
      providers: [
        provideRouter([]),
        { provide: AuthServ, useValue: authStub }
      ]
    })
    .compileComponents();

    fixture = TestBed.createComponent(UserDashboard);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
