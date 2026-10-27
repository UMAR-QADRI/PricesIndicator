import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { AuthServ } from '../auth-serv';
import { AdminDashboard } from './admindashboard';

describe('AdminDashboard', () => {
  let component: AdminDashboard;
  let fixture: ComponentFixture<AdminDashboard>;

  const admin = {
    id: 1,
    name: 'Test Admin',
    email: 'admin@example.com',
    role: 'admin' as const,
    email_verified: true
  };

  const authStub = {
    checkAdminAccess: () => of({ message: 'Admin access verified.', user: admin }),
    getAdminUsers: () => of({
      items: [],
      total: 0,
      page: 1,
      page_size: 10,
      pages: 1
    }),
    clearSession: () => undefined,
    logout: () => undefined
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AdminDashboard],
      providers: [
        provideRouter([]),
        { provide: AuthServ, useValue: authStub }
      ]
    })
    .compileComponents();

    fixture = TestBed.createComponent(AdminDashboard);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
